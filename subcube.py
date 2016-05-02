import numpy as np
import matplotlib.pylab as plt
import astropy.units as u
from astropy import log
from astropy.utils.console import ProgressBar
import pyspeckit
import os

# imports for the test fiteach redefinition
import time, itertools
from astropy.extern.six import string_types

class SubCube(pyspeckit.Cube):
    """
    An extension of Cube, tinkered to be an instance of MultiCube, from which 
    it receives references to instances of pyspeckit.Cube that do not depend 
    on a spectral model chosen (so that parent MultiCube doesn't weigh so much)

    Is designed to have methods that operate within a single spectral model.
    """
    def __init__(self, *args, **kwargs):
        super(SubCube, self).__init__(*args, **kwargs)

        # because that UnitConversionError pops up way too often
        if self.xarr.velocity_convention is None:
            self.xarr.velocity_convention = 'radio'
        
        # so I either define some things as `None` 
        # or I'll have to call hasattr or them...
        # TODO: which is a more Pythonic approach?
        # A: probably the hasattr method, see here:  
        # http://programmers.stackexchange.com/questions/
        # 254576/is-it-a-good-practice-to-declare-instance
        # -variables-as-none-in-a-class-in-python
        self.guess_grid = None
        self.model_grid = None

    def update_model(self, fit_type='gaussian'):
        """
        Tie a model to a SubCube. Should work for all the standard
        fitters; others can be added with Cube.add_fitter method.
        """
        try:
            allowed_fitters = self.specfit.Registry.multifitters
            self.specfit.fitter = allowed_fitters[fit_type]
        except KeyError:
            raise ValueError('Unsupported fit type: %s\n'
                             'Choose one from %s' 
                             % (fit_type, allowed_fitters.keys()))
        log.info("Selected %s model" % fit_type)
        self.specfit.fittype = fit_type
        self.fittype = fit_type

    def make_guess_grid(self, minpars, maxpars, finesse, 
            fixed=None, limitedmin=None, limitedmax=None):
        """
        Given parameter ranges and a finesse parameter, generate a grid of 
        guesses in a parameter space to be iterated upon in self.best_guess
        Maybe if parlimits arg is None we can look into parinfo?

        Parameters
        ----------
        minpars : an iterable containing minimal parameter values

        maxpars : an iterable containing maximal parameter values

        finesse : an integer or 1xNpars list/array setting the size 
                  of cells between minimal and maximal values in 
                  the resulting guess grid

        fixed : an iterable of booleans setting whether or not to fix the
                fitting parameters. Will be passed to Cube.fiteach, defaults
                to an array of False-s.

        limitedmin : an iterable of booleans controlling if the fit fixed
                     the minimal boundary of from minpars.

        limitedmax : an iterable of booleans controlling if the fit fixed
                     the maximal boundary of from maxpars.

        Returns
        -------
        guess_grid : a grid of guesses to use for SubCube.generate_model

        In addition, it saves a number of variables under self as a dictionary
        passed later to Cube.fiteach as additional arguments, with keywords:
        ['fixed', 'limitedmin', 'limitedmax', 'minpars', 'maxpars']
        """
        minpars, maxpars = np.asarray([minpars, maxpars])
        truths, falses = np.ones(minpars.shape, dtype=bool), \
                         np.zeros(minpars.shape, dtype=bool)

        fixed = falses if fixed is None else fixed
        limitedmin = truths if limitedmin is None else limitedmin
        limitedmax = truths if limitedmax is None else limitedmax
        self.fiteach_args = {'fixed'     : fixed,
                             'limitedmin': limitedmin,
                             'limitedmax': limitedmax,
                             'minpars'   : minpars,
                             'maxpars'   : maxpars    }

        # TODO: why does 'fixed' break the gaussian fitter?
        if self.fittype is 'gaussian':
            self.fiteach_args.pop('fixed')
 
        # TODO: make sure you return the same shape!
        input_shape = minpars.shape
        npars = minpars.size

        # conformity for finesse: int or np.array goes in and np.array goes out
        finesse = np.atleast_1d(finesse) * np.ones(npars)

        log.info("Binning the %i-dimensional parameter space into a %s-shaped grid" %
                 (npars, str(tuple(finesse.astype(int)))))

        par_space = []
        for i_len, i_min, i_max in zip(finesse, minpars, maxpars):
            par_space.append(np.linspace(i_min, i_max, i_len))

        nguesses = np.prod(map(len,par_space))
        guess_grid = np.array(np.meshgrid(*par_space)).reshape(npars, nguesses).T
        
        # this flips all the dimensions, might have some troubles should 
        # I want to scale this up later for a multidimensional case
        self.guess_grid = guess_grid

        return guess_grid

    def generate_model(self, guess_grid=None, to_file=None, redo=True):
        """
        Generates a grid of spectral models matching the
        shape of the input guess_grid array. Can take the
        following numpy arrays as an input:

        Parameters
        ----------
        guess_grid : numpy.array
                     A grid of input parameters. 
                     Can be one of the following:
                     1) An (M,)-shaped 1d array of model parameters
                        (see pyspeckit docs for details)
                     2) An (N, M) array to compute N models 
                        for M sets of parameters
                     3) A guess cube of (Y, X, M) size
                     4) An (N, Y, X, M)-shaped array, to 
                        iterate over cubes of guesses.
                     If not set, SubCube.guess_grid is used.

        to_file : string; if not None then the models generated will
                  be saved to an .npy file instead of class attribute

        redo : boolean; if False and to_file filename is in place, the
               model gird will not be generated anew
        """

        if not redo and os.path.isfile(to_file):
            log.info("A file with generated models is "
                     "already in place. Skipping.")
            return

        if guess_grid is None:
            try:
                guess_grid = self.guess_grid
            except AttributeError:
                raise RuntimeError("Can't find the guess grid to use,")

        # safeguards preventing wrong output shapes
        npars = self.specfit.fitter.npars
        guess_grid = np.atleast_2d(guess_grid)
        grid_shape = guess_grid.shape[:-1]
        if ((len(grid_shape)>1 and grid_shape[-2:]!=self.cube.shape[1:]) or
            (len(grid_shape)>3) or (guess_grid.shape[-1]%npars)):
            raise ValueError("Invalid shape for the guess_grid, "
                             "check the docsting for details.")

        model_grid = np.empty(shape=grid_shape+(self.xarr.size,))
        # NOTE: this for loop is the performance bottleneck!
        # would be nice if I could broadcast guess_grid to n_modelfunc...
        log.info("Generating spectral models from the guess grid . . .")
        with ProgressBar(model_grid.shape[0]) as bar:
            for idx in np.ndindex(grid_shape):
                model_grid[idx] = \
                    self.specfit.get_full_model(pars=guess_grid[idx])
                bar.update()

        if to_file is not None:
            np.save(to_file, model_grid)
        else:
            self.model_grid = model_grid

    def best_guess(self, model_grid=None, sn_cut=None,
                   memory_limit=None, from_file=None, **kwargs):
        """
        For a grid of initial guesses, determine the optimal one based 
        on the preliminary residual of the specified spectral model.

        Parameters
        ----------
        model_grid : numpy.array; A model grid to choose from.

        use_cube : boolean; If true, every xy-slice of a cube will
                   be compared to every model from the model_grid.
                   sn_cut (see below) is still applied.

        sn_cut : float; do not consider model selection for pixels
                 below this signal-to-noise ratio cutoff.

        memory_limit : float; How many gigabytes of RAM could be used for
                       broadcasting. If estimated usage goes over this
                       number, best_guess switches to a slower method.

        from_file : string; if not None then the models grid will be
                    read from a file using np.load, which additional
                    arguments, like mmap_mode, passed along to it

        Output
        ------
        best_guesses : a cube of best models corresponding to xy-grid
                       (saved as a SubCube attribute)

        best_guess : a most commonly found best guess

        best_snr_guess : the model for the least residual at peak SNR
                         (saved as a SubCube attribute)

        """
        if model_grid is None:
            if from_file is not None:
                model_grid = np.load(from_file, **kwargs)
            elif self.model_grid is None:
                raise TypeError('sooo the model_grid is empty, '
                                'did you run generate_model()?')
            else:
                model_grid = self.model_grid

        # TODO: allow for all the possible outputs from generate_model()
        if model_grid.shape[-1]!=self.cube.shape[0]:
            raise ValueError("Invalid shape for the guess_grid, "
                             "check the docsting for details.")
        if len(model_grid.shape)>2:
            raise NotImplementedError("Complex model girds aren't supported.")

        log.info("Calculating residuals for generated models . . .")

        try: # TODO: move this out into an astro_toolbox function
            import psutil
            mem = psutil.virtual_memory().available
        except ImportError:
            import os
            try:
                memgb = os.popen("free -g").readlines()[1].split()[3]
            except IndexError: # would happen on Macs/Windows
                memgb = 8
                log.warn("Can't get the free RAM "
                         "size, assuming %i GB" % memgb)
            memgb = memory_limit or memgb
            mem = int(memgb) * 2**30

        # allow for 50% computational overhead
        threshold = self.cube.nbytes*model_grid.shape[0]*2
        if mem < threshold:
            log.warn("The available free memory might not be enough for "
                     "broadcasting model grid to the spectral cube. Will "
                     "iterate over all the XY pairs instead. Coffee time!")

            try:
                if type(model_grid) is not np.ndarray: # assume memmap type
                    raise MemoryError("This will take ages, skipping to "
                                      "the no-broadcasting scenario.")
                residual_rms = np.empty(shape=((model_grid.shape[0],)+
                                                self.cube.shape[1:]   ))
                with ProgressBar(np.prod(self.cube.shape[1:])) as bar:
                    for (y,x) in np.ndindex(self.cube.shape[1:]):
                        residual_rms[:,y,x] = (self.cube[None,:,y,x] -
                                                    model_grid).std(axis=1)
                        bar.update()
            except MemoryError: # catching memory errors could be really bad!
                log.warn("Not enough memory to broadcast model grid to the "
                         "XY grid. This is bad for a number of reasons, the "
                         "formost of which: the running time just went "
                         "through the roof. Leave it overnight maybe?")
                best_map = np.empty(shape=(self.cube.shape[1:]))
                rmsmin_map = np.empty(shape=(self.cube.shape[1:]))
                if sn_cut:
                    snr_mask = self.snr_map > sn_cut
                # TODO: this takes ages! refactor this through hdf5
                # "chunks" of acceptable size, and then broadcast them!
                with ProgressBar(np.prod((model_grid.shape[0],)+
                                          self.cube.shape[1:]  )) as bar:
                    for (y,x) in np.ndindex(self.cube.shape[1:]):
                        if sn_cut:
                            if not snr_mask[y,x]:
                                best_map[y,x],rmsmin_map[y,x] = np.nan,np.nan
                                continue
                        resid_rms_xy = np.empty(shape=model_grid.shape[0])
                        for model_id in np.ndindex(model_grid.shape[0]):
                            resid_rms_xy[model_id] = (self.cube[:,y,x] -
                                                  model_grid[model_id]).std()
                            bar.update()
                        best_map[y,x] = np.argmin(resid_rms_xy)
                        rmsmin_map[y,x] = np.nanmin(resid_rms_xy)
        else:
            # NOTE: broadcasting below is a much faster way to compute
            #       cube - model residuals. But for big model sizes this
            #       will cause memory overflows.
            #       The code above tried to catch this before it happens
            #       and run things in a slower fashion.
            residual_rms = (self.cube[None,:,:,:]-
                                model_grid[:,:,None,None]).std(axis=1)

        if sn_cut:
            snr_mask = self.snr_map > sn_cut
            residual_rms[self.get_slice_mask(snr_mask)] = np.inf

        best_map   = np.argmin(residual_rms, axis=0)
        rmsmin_map = residual_rms.min(axis=0)
        self._best_map    = best_map
        self._best_rmsmap = rmsmin_map
        self.best_guesses = np.rollaxis(self.guess_grid[best_map],-1)

        from scipy.stats import mode
        model_mode = mode(best_map)
        best_model_num = model_mode[0][0,0]
        best_model_freq = model_mode[1][0,0]
        best_model_frac = (float(best_model_freq) /
                            np.prod(self.cube.shape[1:]))
        if best_model_frac < .05:
            log.warn("Selected model is best only for less than %5 "
                     "of the cube, consider using the map of guesses.")
        self._best_model = best_model_num
        self.best_guess  = self.guess_grid[best_model_num]
        log.info("Overall best model: selected #%i %s" % (best_model_num,
                 self.guess_grid[best_model_num].round(2)))

        try:
            best_snr = np.argmax(self.snr_map)
            best_snr = np.unravel_index(best_snr, self.snr_map.shape)
            self.best_snr_guess = self.guess_grid[best_map[best_snr]]
            log.info("Best model @ highest SNR: #%i %s" %
                     (best_map[best_snr], self.best_snr_guess.round(2)))
        except AttributeError:
            log.warn("Can't find the SNR map, best guess at "
                     "highest SNR pixel will not be stored.")

    def get_slice_mask(self, mask2d):
        """
        In case we ever want to apply a 2d mask to a whole cube.
        """
        mask3d = np.repeat([mask2d],self.xarr.size,axis=0)
        return mask3d

    def get_snr_map(self, signal=None, noise=None, unit='km/s', 
                    signal_mask=None, noise_mask=None          ):
        """
        Calculates S/N ratio for the cube. If no information is given on where
        to look for signal and noise channels, a (more-or-less reasonable) rule
        of thirds is used: the outer thirds of the channel range are used to 
        get the root mean square of the noise, and the max value in the inner 
        third is assumed to be the signal strength.
        
        Parameters
        ----------
        signal : 2xN numpy.array, where N is the total number of signal blocks.
                 Should contain channel numbers in `unit` convention, the first
                 subarray for start of the signal block and the second one for
                 the end of the signal block

        noise : 2xN numpy.array, where N is the total number of noise blocks.
                Same as `signal` otherwise.

        unit : a unit for specifying the channels. Defaults to 'km/s'.
               If set to 'pixel', actual channel numbers are selected.

        signal_mask : dtype=bool numpy.array of SubCube.xarr size
                      If specified, used as a mask to get channels with signal.
                      Overrules `signal`

        noise_mask : dtype=bool numpy.array of SubCube.xarr size
                     If specified, used as a mask to get channels with noise.
                     Overrules `noise`

        Returns
        -------
        snr_map : numpy.array
                  Also stored under SubCube.snr_map
        """
        # will override this later if no ranges were actually given
        unit = {'signal': unit, 'noise': unit}

        # get rule of thirds signal and noise if no ranges were given
        default_cut = 0.33
        if signal is None:
            # find signal cuts for the current unit?
            # nah let's just do it in pixels, shall we?
            i_low, i_high = int(round(self.xarr.size *    default_cut )),\
                            int(round(self.xarr.size * (1-default_cut)))
            signal = [[i_low+1], [i_high-1]]
            unit['signal'] = 'pixel'

        if noise is None:
            # find signal cuts for the current unit?
            # nah let's just do it in pixels, shall we?
            i_low, i_high = int(round(self.xarr.size *    default_cut )),\
                            int(round(self.xarr.size * (1-default_cut)))
            noise = [[0, i_high], [i_low, self.xarr.size-1]]
            unit['noise'] = 'pixel'

        # setting xarr masks from high / low indices
        if signal_mask is None:
            signal_mask = self.get_mask(*signal, unit=unit['signal'])
        if noise_mask is None:
            noise_mask = self.get_mask(*noise, unit=unit['noise'])
        self._mask_signal = signal_mask
        self._mask_noise = noise_mask

        # no need to care about units at this point
        snr_map = self.get_signal_map(signal_mask) / \
                             self.get_rms_map(noise_mask)
        self.snr_map = snr_map
        return snr_map

    def get_mask(self, low_indices, high_indices, unit):
        """
        Converts low / high indices arrays into a mask on self.xarr
        """
        mask = np.array([False]*self.xarr.size)
        for low, high in zip(low_indices, high_indices):
            # you know this is a hack right?
            # also, undocumented functionality is bad and you should feel bad
            if unit not in ['pix','pixel','pixels','chan','channel','channels']:
                # converting whatever units we're given to pixels
                unit_low, unit_high = low*u.Unit(unit), high*u.Unit(unit)
                try:
                    # FIXME: this is too slow, find a better way!
                    unit_bkp = self.xarr.unit
                    self.xarr.convert_to_unit(unit)
                except u.core.UnitConversionError as err:
                    raise type(err)(str(err) + "\nConsider setting, e.g.:\n"
                            "SubCube.xarr.velocity_convention = 'radio'\n"
                            "and\nSubCube.xarr.refX = line_freq*u.GHz")
                index_low  = self.xarr.x_to_pix(unit_low)
                index_high = self.xarr.x_to_pix(unit_high)
                self.xarr.convert_to_unit(unit_bkp)
            else: 
                try:
                    index_low, index_high = int(low.value ),\
                                            int(high.value)
                except AttributeError:
                    index_low, index_high = int(low), int(high)

            # so this also needs to be sorted if the axis goes in reverse
            index_low, index_high = np.sort([index_low, index_high])

            mask[index_low:index_high] = True

        return mask

    def get_rms_map(self, noise_mask=None):
        """
        Make an rms estimate, will try to find the noise channels in
        the input values or in class instances. If noise mask is not
        not given, defaults to calculating rms of all channels.

        Parameters
        ----------
        noise_mask : dtype=bool numpy.array of SubCube.xarr size
                     If specified, used as a mask to get channels with noise.

        Returns
        -------
        rms_map : numpy.array, also stored under SubCube.rms_map
        """
        if noise_mask is None:
            log.warn('no noise mask was given, will calculate the RMS '
                     'over all channels, thus overestimating the noise!')
            noise_mask = np.ones(self.xarr.shape, dtype=bool)
        rms_map = self.cube[noise_mask,:,:].std(axis=0)
        self._rms_map = rms_map
        return rms_map

    def get_signal_map(self, signal_mask=None):
        """
        Make a signal strength estimate. If signal mask is not
        not given, defaults to maximal signal on all channels.

        Parameters
        ----------
        signal_mask : dtype=bool numpy.array of SubCube.xarr size
                      If specified, used as a mask to get channels with signal.

        Returns
        -------
        signal_map : numpy.array, also stored under SubCube.signal_map
        """
        if signal_mask is None:
            log.warn('no signal mask was given, will calculate the signal '
                     'over all channels: true signal might be lower.')
            signal_mask = np.array(self.xarr.shape, dtype=bool)
        signal_map = self.cube[signal_mask,:,:].max(axis=0)
        self._signal_map = signal_map
        return signal_map

    def get_chi_squared(self, sigma = None, refresh=False):
        """
        Computes a chi-squared map from modelcube / parinfo.
        """
        if self._modelcube is None or refresh:
            self.get_modelcube()

        if sigma is None:
            sigma = self._rms_map

        chisq = ((self.cube - self._modelcube)**2).sum(axis=0)/sigma**2

        self.chi_squared = chisq
        return chisq

    def chi_squared_stats(self, plot_chisq=False):
        """
        Compute chi^2 statistics for an X^2 distribution.
        This is essentially a chi^2 test for normality being
        computed on residual from the fit. I'll rewrite it 
        into a chi^2 goodness of fit test when I'll get around
        to it.

        Returns
        -------
        prob_chisq : probability that X^2 obeys the chi^2 distribution

        dof : degrees of freedom for chi^2
        """
        # ------------------- TODO --------------------- #
        # rewrite it to a real chi-square goodness of fit!
        # this is essentially a chi^2 test for normality
        from scipy.stats import chisqprob

        # TODO: for Pearson's chisq test it would be
        # dof = self.xarr.size - self.specfit.fitter.npars - 1
        
        # NOTE: likelihood function should asymptotically approach
        #       chi^2 distribution too! Given that the whole point
        #       of calculating chi^2 is to use it for model 
        #       selection I should probably switch to it.

        # TODO: derive an expression for this "Astronomer's X^2" dof.
        dof = self.xarr.size
        prob_chisq = chisqprob(self.chi_squared, dof)

        # NOTE: for some reason get_modelcube returns zeros for some
        #       pixels even if corresponding Cube.parcube[:,y,x] is NaN
        prob_chisq[np.isnan(self.parcube.min(axis=0))] = np.nan

        if plot_chisq:
            if not plt.rcParams['text.usetex']:
                plt.rc('text', usetex=True)
            if self.mapplot.figure is None:
                self.mapplot()
            self.mapplot.plane = prob_chisq
            self.mapplot(estimator=None, cmap='viridis', vmin=0, vmax=1)
            labtxt = r'$\chi^2\mathrm{~probability~(%i~d.o.f.)}$' % dof
            self.mapplot.FITSFigure.colorbar.set_axis_label_text(labtxt)
            plt.show()

        self.prob_chisq = prob_chisq

        return prob_chisq, dof

    def mark_bad_fits(self, ax = None, mask = None, 
                      cut = 1e-20, method = 'cross', **kwargs):
        """
        Given an active axis used by Cube.mapplot, overplot 
        pixels with bad fits with an overlay.

        Can pass along a mask of bad pixels; if none is given 
        the method tries to get its own guess from:
        self.prob_chisq < cut

        Additional keyword arguments are passed to plt.plot.
        """
        # setting defaults for plotting if no essentials are passed
        ax = ax or self.mapplot.axis
        pltkwargs = {'alpha': 0.8, 'ls': '--', 'lw': 1.5, 'c': 'r'}
        pltkwargs.update(kwargs)
        # because the plotting routine would attempt to change the scale
        try:
            ax.autoscale(False)
        except AttributeError:
            raise RuntimeError("Can't find an axis to doodle on.")

        # NOTE: this would only work for a singular component
        #       due to the way we're calculating X^2. One can,
        #       in principle, calculate X^2 with a mask to
        #       bypass this issue, but only in the case of the
        #       components being clearly separated.
        #       Otherwise the cut value needs to be set "by eye"
        mask = self.prob_chisq < cut if self.prob_chisq is not None else mask

        # that +1 modifier is there because of aplpy's
        # convention on the (0,0) origin in FITS files
        for y,x in np.stack(np.where(mask)).T+1:
            self._doodle_xy(ax, (x,y), method, **pltkwargs)

    def _doodle_xy(self, ax, xy, method, **kwargs):
        """
        Draws lines on top of a pixel.

        Parameters
        ----------
        ax : axis to doodle on

        xy : a tuple of xy coordinate pair

        method : what to draw. 'box' and 'cross' are supported
        """
        x, y = xy
        if method is 'box':
            ax.plot([x-.5,x-.5,x+.5,x+.5,x-.5], 
                    [y-.5,y+.5,y+.5,y-.5,y-.5], 
                    **kwargs)
        elif method is 'cross':
            ax.plot([x-.5,x+.5], [y-.5,y+.5], **kwargs)
            ax.plot([x+.5,x-.5], [y-.5,y+.5], **kwargs)
        else:
            raise ValueError("unknown method %s passed to "
                             "the doodling function" % method)

    def get_likelihood(self, sigma = None):
        """
        Computes log-likelihood map from chi-squared
        """
        # self-NOTE: need to deal with chi^2 first
        raise NotImplementedError
    #    if sigma is None:
    #        sigma = self._rms_map

    #    # TODO: resolve extreme exponent values or risk overflowing
    #    likelihood=np.exp(-self.chi_squared/2)* \
    #           (sigma*np.sqrt(2*np.pi))**(-self.xarr.size)
    #    self.likelihood = np.log(likelihood)

    #    return np.log(likelihood)

    # taken directly from pyspeckit.cubes.fiteach()!
    # reduced the function a bit, will tiner 
    # dynamically selected guesses soon.

    def fiteach(self, errmap=None, guesses=(), verbose=True, verbose_level=1,
                quiet=True, signal_cut=3, usemomentcube=None, blank_value=0,
                use_neighbor_as_guess=False, use_best_as_guess=False,
                start_from_point=(0,0), multicore=1, position_order=None,
                maskmap=None, **fitkwargs):
        """
        Fit a spectrum to each valid pixel in the cube

        For guesses, priority is *use_nearest_as_guess*, *usemomentcube*,
        *guesses*, None

        Once you have successfully run this function, the results will be
        stored in the ``.parcube`` and ``.errcube`` attributes, which are each
        cubes of shape ``[npars, ny, nx]``, where npars is the number of fitted
        parameters and ``nx``, ``ny`` are the shape of the map.  ``errcube``
        contains the errors on the fitted parameters (1-sigma, as returned from
        the Levenberg-Marquardt fit's covariance matrix).  You can use the
        attribute ``has_fit``, which is a map of shape ``[ny,nx]`` to find
        which pixels have been successfully fit.


        Parameters
        ----------
        use_neighbor_as_guess: bool
            Set this keyword to use the average best-fit parameters from
            neighboring positions with successful fits as the guess
        use_best_as_guess: bool
            If true, the initial guess for the pixel is selected as the one
            giving the least residual among the fits from the neighboring
            pixels and the guess for the pixel
        start_from_point: tuple(int,int)
            Either start from the center or from a point defined by a tuple.
            Work outward from that starting point.
        position_order: ndarray[naxis=2]
            2D map of region with pixel values indicating the order in which
            to carry out the fitting.  Any type with increasing pixel values.
        guesses: tuple or ndarray[naxis=3]
            Either a tuple/list of guesses with len(guesses) = npars or a cube
            of guesses with shape [npars, ny, nx].
        signal_cut: float
            Minimum signal-to-noise ratio to "cut" on (i.e., if peak in a given
            spectrum has s/n less than this value, ignore it)
        blank_value: float
            Value to replace non-fitted locations with.  A good alternative is
            numpy.nan
        verbose: bool
        verbose_level: int
            Controls how much is output.
            0,1 - only changes frequency of updates in loop
            2 - print out messages when skipping pixels
            3 - print out messages when fitting pixels
            4 - specfit will be verbose
        multicore: int
            if >1, try to use multiprocessing via parallel_map to run on
            multiple cores
        maskmap : `np.ndarray`, optional
            A boolean mask map, where ``True`` implies that the data are good.
            This will be used for both plotting using `mapplot` and fitting
            using `fiteach`.  If ``None``, will use ``self.maskmap``.
        """
        if not hasattr(self.mapplot,'plane'):
            self.mapplot.makeplane()

        if maskmap is None:
            maskmap = self.maskmap

        yy,xx = np.indices(self.mapplot.plane.shape)
        if isinstance(self.mapplot.plane, np.ma.core.MaskedArray):
            OK = ((~self.mapplot.plane.mask) &
                  maskmap.astype('bool')).astype('bool')
        else:
            OK = (np.isfinite(self.mapplot.plane) &
                  maskmap.astype('bool')).astype('bool')

        # NAN guesses rule out the model too
        if hasattr(guesses,'shape') and guesses.shape[1:] == self.cube.shape[1:]:
            bad = np.isnan(guesses).sum(axis=0).astype('bool')
            OK &= (~bad)

        if start_from_point == 'center':
            start_from_point = (xx.max()/2., yy.max()/2.)
        if hasattr(position_order,'shape') and position_order.shape == self.cube.shape[1:]:
            sort_distance = np.argsort(position_order.flat)
        else:
            d_from_start = ((xx-start_from_point[1])**2 + (yy-start_from_point[0])**2)**0.5
            sort_distance = np.argsort(d_from_start.flat)

        valid_pixels = list(zip(xx.flat[sort_distance][OK.flat[sort_distance]],
                                yy.flat[sort_distance][OK.flat[sort_distance]]))

        if len(valid_pixels) != len(set(valid_pixels)):
            raise ValueError("There are non-unique pixels in the 'valid pixel' list.  "
                             "This should not be possible and indicates a major error.")
        elif len(valid_pixels) == 0:
            raise ValueError("No valid pixels selected.")

        if verbose_level > 0:
            log.debug("Number of valid pixels: %i" % len(valid_pixels))

        guesses_are_moments = (isinstance(guesses, string_types) and
                                 guesses in ('moment','moments'))
        if guesses_are_moments or (usemomentcube and len(guesses)):
            if not hasattr(self, 'momentcube') and guesses_are_moments:
                self.momenteach()
            npars = self.momentcube.shape[0]
        else:
            npars = len(guesses)
            if npars == 0:
                raise ValueError("Parameter guesses are required.")

        self.parcube = np.zeros((npars,)+self.mapplot.plane.shape)
        self.errcube = np.zeros((npars,)+self.mapplot.plane.shape)

        # newly needed as of March 27, 2012.  Don't know why.
        if 'fittype' in fitkwargs:
            self.specfit.fittype = fitkwargs['fittype']
        self.specfit.fitter = self.specfit.Registry.multifitters[self.specfit.fittype]

        # TODO: VALIDATE THAT ALL GUESSES ARE WITHIN RANGE GIVEN THE
        # FITKWARG LIMITS

        # array to store whether pixels have fits
        self.has_fit = np.zeros(self.mapplot.plane.shape, dtype='bool')

        self._counter = 0

        t0 = time.time()

        def fit_a_pixel(iixy):
            ii,x,y = iixy
            sp = self.get_spectrum(x,y)

            # very annoying - cannot use min/max without checking type
            # maybe can use np.asarray here?
            # cannot use sp.data.mask because it can be a scalar boolean,
            # which does unpredictable things.
            if hasattr(sp.data, 'mask') and not isinstance(sp.data.mask, (bool,
                                                                          np.bool_)):
                sp.data[sp.data.mask] = np.nan
                sp.error[sp.data.mask] = np.nan
                sp.data = np.array(sp.data)
                sp.error = np.array(sp.error)

            elif errmap is not None:
                sp.error = np.ones(sp.data.shape) * errmap[y,x]
            else:
                if verbose_level > 1 and ii==0:
                    log.warn("using data std() as error.")
                sp.error[:] = sp.data[sp.data==sp.data].std()
            if sp.error is not None and signal_cut > 0:
                snr = sp.data / sp.error
                max_sn = np.nanmax(snr)
                if max_sn < signal_cut:
                    if verbose_level > 1:
                        log.info("Skipped %4i,%4i (s/n=%0.2g)" % (x,y,max_sn))
                    return
                elif np.isnan(max_sn):
                    if verbose_level > 1:
                        log.info("Skipped %4i,%4i (s/n is nan; max(data)=%0.2g, min(error)=%0.2g)" %
                                 (x,y,np.nanmax(sp.data),np.nanmin(sp.error)))
                    return
                if verbose_level > 2:
                    log.info("Fitting %4i,%4i (s/n=%0.2g)" % (x,y,max_sn))
            else:
                max_sn = None
            sp.specfit.Registry = self.Registry # copy over fitter registry

            # Do some homework for local fits
            # Exclude out of bounds points
            xpatch, ypatch = get_neighbors(x,y,self.has_fit.shape)
            local_fits = self.has_fit[ypatch+y,xpatch+x]

            if use_best_as_guess and np.any(local_fits):
                gg = guesses[:,y,x] if len(guesses.shape)>1 else guesses
                near_guesses = self.parcube[:, (ypatch+y)[local_fits],
                                               (xpatch+x)[local_fits] ].T
                ggrid = np.vstack([gg, near_guesses])
                resid = [(sp.data - sp.specfit.get_full_model(pars=iguess)).std()
                            for iguess in ggrid]
                gg = ggrid[np.argmin(resid)]
                if np.argmin(resid):
                    log.info("Selecting best guess at (%i,%i) from (%i,%i): %s" %
                             (x,y,xpatch[np.argmin(resid)-1]+x,
                                  ypatch[np.argmin(resid)-1]+y,str(gg)))
                else:
                    log.info("Selecting best guess from input guess.")
            elif use_neighbor_as_guess and np.any(local_fits):
                # Array is N_guess X Nvalid_nbrs so averaging over
                # Axis=1 is the axis of all valid neighbors
                gg = np.mean(self.parcube[:, (ypatch+y)[local_fits],
                                          (xpatch+x)[local_fits]], axis=1)
            elif hasattr(guesses,'shape') and guesses.shape[1:] == self.cube.shape[1:]:
                if verbose_level > 1 and ii == 0:
                    log.info("Using input guess cube")
                gg = guesses[:,y,x]
            else:
                if verbose_level > 1 and ii == 0:
                    log.info("Using input guess")
                gg = guesses

            if np.all(np.isfinite(gg)):
                try:
                    sp.specfit(guesses=gg, quiet=verbose_level<=3,
                               verbose=verbose_level>3, **fitkwargs)
                    self.parcube[:,y,x] = sp.specfit.modelpars
                    self.errcube[:,y,x] = sp.specfit.modelerrs
                    success = True
                except Exception as ex:
                    log.exception("Fit number %i at %i,%i failed on error %s" % (ii,x,y, str(ex)))
                    log.exception("Guesses were: {0}".format(str(gg)))
                    log.exception("Fitkwargs were: {0}".format(str(fitkwargs)))
                    success = False
                    if isinstance(ex,KeyboardInterrupt):
                        raise ex

                # keep this out of the 'try' statement
                self.has_fit[y,x] = success
            else:
                self.has_fit[y,x] = False
                self.parcube[:,y,x] = blank_value
                self.errcube[:,y,x] = blank_value


            if blank_value != 0:
                self.parcube[self.parcube == 0] = blank_value
                self.errcube[self.parcube == 0] = blank_value

            self._counter += 1
            if verbose:
                if ii % (min(10**(3-verbose_level),1)) == 0:
                    snmsg = " s/n=%5.1f" % (max_sn) if max_sn is not None else ""
                    npix = len(valid_pixels)
                    pct = 100 * (ii+1.0)/float(npix)
                    log.info("Finished fit %6i of %6i at (%4i,%4i)%s. Elapsed time is %0.1f seconds.  %%%01.f" %
                             (ii+1, npix, x, y, snmsg, time.time()-t0, pct))

            if sp.specfit.modelerrs is None:
                log.exception("Fit number %i at %i,%i failed with no specific error." % (ii,x,y))
                log.exception("Guesses were: {0}".format(str(gg)))
                log.exception("Fitkwargs were: {0}".format(str(fitkwargs)))
                raise TypeError("The fit never completed; something has gone wrong.")

            return ((x,y), sp.specfit.modelpars, sp.specfit.modelerrs)

        if multicore > 1:
            sequence = [(ii,x,y) for ii,(x,y) in tuple(enumerate(valid_pixels))]
            result = pyspeckit.cubes.parallel_map(fit_a_pixel, sequence, numcores=multicore)
            self._result = result # backup - don't want to lose data in the case of a failure
            # a lot of ugly hacking to deal with the way parallel_map returns
            # its results needs TWO levels of None-filtering, because any
            # individual result can be None (I guess?) but apparently (and this
            # part I don't believe) any individual *fit* result can be None as
            # well (apparently the x,y pairs can also be None?)
            merged_result = [core_result for core_result in result if
                             core_result is not None]
            # for some reason, every other time I run this code, merged_result
            # ends up with a different intrinsic shape.  This is an attempt to
            # force it to maintain a sensible shape.
            try:
                ((x,y), m1, m2) = merged_result[0]
            except ValueError:
                if verbose > 1:
                    log.exception("ERROR: merged_result[0] is {0} which has the"
                                  " wrong shape".format(merged_result[0]))
                merged_result = itertools.chain.from_iterable(merged_result)
            for TEMP in merged_result:
                if TEMP is None:
                    # this shouldn't be possible, but it appears to happen
                    # anyway.  parallel_map is great, up to a limit that was
                    # reached long before this level of complexity
                    log.debug("Skipped a None entry: {0}".format(str(TEMP)))
                    continue
                try:
                    ((x,y), modelpars, modelerrs) = TEMP
                except TypeError:
                    # implies that TEMP does not have the shape ((a,b),c,d)
                    # as above, shouldn't be possible, but it happens...
                    log.debug("Skipped a misshapen entry: {0}".format(str(TEMP)))
                    continue
                if ((len(modelpars) != len(modelerrs)) or
                    (len(modelpars) != len(self.parcube))):
                    raise ValueError("There was a serious problem; modelpar and"
                                     " error shape don't match that of the "
                                     "parameter cubes")
                if np.any(np.isnan(modelpars)) or np.any(np.isnan(modelerrs)):
                    self.parcube[:,y,x] = np.nan
                    self.errcube[:,y,x] = np.nan
                    self.has_fit[y,x] = False
                else:
                    self.parcube[:,y,x] = modelpars
                    self.errcube[:,y,x] = modelerrs
                    self.has_fit[y,x] = max(modelpars) > 0
        else:
            for ii,(x,y) in enumerate(valid_pixels):
                fit_a_pixel((ii,x,y))

        sp = self.get_spectrum(*valid_pixels[0])
        sp.specfit(guesses = self.parcube[:,
                    valid_pixels[0][1],valid_pixels[0][0]],
                    fittype = self.specfit.fittype)
        # March 27, 2014: This is EXTREMELY confusing.  This isn't in a loop...
        # make sure the fitter / fittype are set for the cube
        # this has to be done within the loop because skipped-over spectra
        # don't ever get their fittypes set
        self.specfit.fitter = sp.specfit.fitter
        self.specfit.fittype = sp.specfit.fittype
        self.specfit.parinfo = sp.specfit.parinfo

        if verbose:
            log.info("Finished final fit %i.  "
                     "Elapsed time was %0.1f seconds" % (len(valid_pixels), time.time()-t0))


class SubCubeStack(SubCube, pyspeckit.CubeStack):
    """
    SubCube analogy for CubeStack objects.
    """
    def __init__(self,*args):
        super(SubCubeStack, self).__init__(*args)
        log.warn("Doing something horrible with multiple "
                 "inheritance. Needs to be tested more.")

# taken directly from pyspeckit.cubes, 
# I can't seem to import it for some reason
def get_neighbors(x, y, shape):
    """
    Find the 9 nearest neighbors, excluding self and any out of bounds points
    """
    ysh, xsh = shape
    xpyp = [(ii,jj)
            for ii,jj in itertools.product((-1,0,1),
                                           (-1,0,1))
            if (ii+x < xsh) and (ii+x >= 0)
            and (jj+y < ysh) and (jj+y >= 0)
            and not (ii==0 and jj==0)]
    xpatch, ypatch = zip(*xpyp)

    return np.array(xpatch, dtype='int'), np.array(ypatch, dtype='int')


