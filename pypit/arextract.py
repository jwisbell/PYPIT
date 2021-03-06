from __future__ import (print_function, absolute_import, division, unicode_literals)

import copy
import numpy as np
from astropy import units as u
from pypit import armsgs
from pypit import arparse as settings
from pypit import artrace
from pypit import arutils
from pypit import arqa

# Logging
msgs = armsgs.get_logger()

from pypit import ardebug as debugger

# MASK VALUES FROM EXTRACTION
# 0 
# 2**0 = Flagged as bad detector pixel
# 2**1 = Flagged as affected by Cosmic Ray 
# 2**5 = Flagged as NAN (from something gone wrong)

mask_flags = dict(bad_pix=2**0, CR=2**1, NAN=2**5)


def boxcar(slf, det, specobjs, sciframe, varframe, skyframe, crmask, scitrace):
    """ Perform boxcar extraction on the traced objects.
    Also perform a local sky subtraction

    Parameters
    ----------
    det : int
      Detector index
    specobjs : list of dict
      list of SpecObj objects
    sciframe : ndarray
      science frame
    varframe : ndarray
      variance image
    bgframe : ndarray
      sky background frame
    crmask : int ndarray
        mask of cosmic ray hits
    scitrace : dict
      traces, object and background trace images

    Returns
    -------
    bgcorr : ndarray
      Correction to the sky background in the object window
    """
    from pypit import arcyutils
    from astropy.stats import sigma_clip

    bgfitord = 1  # Polynomial order used to fit the background
    nslit = len(scitrace)
    cr_mask = 1.0-crmask
    bgfit = np.linspace(0.0, 1.0, sciframe.shape[1])
    bgcorr = np.zeros_like(cr_mask)
    # Loop on Slits
    for sl in range(nslit):
        word = np.where(slf._slitpix[det - 1] == sl + 1)
        mask_slit = np.zeros(sciframe.shape, dtype=np.float)
        mask_slit[word] = 1.0
        # Loop on Objects
        nobj = scitrace[sl]['nobj']
        for o in range(nobj):
            msgs.info("Performing boxcar extraction of object {0:d}/{1:d} in slit {2:d}/{3:d}".format(o+1, nobj, sl+1, nslit))
            if scitrace[sl]['object'] is None:
                # The object for all slits is provided in the first extension
                objreg = np.copy(scitrace[0]['object'][:, :, o])
                wzro = np.where(slf._slitpix[det - 1] != sl + 1)
                objreg[wzro] = 0.0
            else:
                objreg = scitrace[sl]['object'][:, :, o]
            # Fit the background
            msgs.info("   Fitting the background")
            if scitrace[sl]['background'] is None:
                # The background for all slits is provided in the first extension
                bckreg = np.copy(scitrace[0]['background'][:, :, o])
                wzro = np.where(slf._slitpix[det - 1] != sl + 1)
                bckreg[wzro] = 0.0
            else:
                bckreg = scitrace[sl]['background'][:, :, o]
            # Trim CRs further
            bg_mask = np.zeros_like(sciframe)
            bg_mask[np.where((bckreg*cr_mask <= 0.))] = 1.
            bg_mask[np.where((slf._slitpix[det - 1] != sl + 1))] = 1.
            mask_sci = np.ma.array(sciframe, mask=bg_mask, fill_value=0.)
            clip_image = sigma_clip(mask_sci, axis=1, sigma=3.)  # For the mask only
            # Fit
            bgframe = arcyutils.func2d_fit_val(bgfit, sciframe, (~clip_image.mask)*bckreg*cr_mask, bgfitord)
            # Weights
            weight = objreg*mask_slit
            sumweight = np.sum(weight, axis=1)
            # Generate wavelength array (average over the pixels)
            wvsum = np.sum(slf._mswave[det-1]*weight, axis=1)
            wvsum /= sumweight
            # Generate sky spectrum (flux per pixel)
            skysum = np.sum(skyframe*weight, axis=1)
            skysum /= sumweight
            # Total the object flux
            msgs.info("   Summing object counts")
            scisum = np.sum((sciframe-bgframe)*weight, axis=1)
            # Total the variance array
            msgs.info("   Summing variance array")
            varsum = np.sum(varframe*weight, axis=1)
            # Update background correction image
            tmp = bckreg + objreg
            gdp = np.where((tmp > 0) & (slf._slitpix[det - 1] == sl + 1))
            bgcorr[gdp] = bgframe[gdp]
            # Mask
            boxmask = np.zeros(wvsum.shape, dtype=np.int)
            # Bad detector pixels
            BPs = np.sum(weight*slf._bpix[det-1], axis=1)
            bp = BPs > 0.
            boxmask[bp] += mask_flags['bad_pix']
            # CR
            CRs = np.sum(weight*cr_mask, axis=1)
            cr = CRs > 0.
            boxmask[cr] += mask_flags['CR']
            # NAN
            NANs = np.isnan(scisum)
            if np.sum(NANs) > 0:
                msgs.warn("   NANs in the spectrum somehow...")
                boxmask[NANs] += mask_flags['NANs']
                scisum[NANs] = 0.
                varsum[NANs] = 0.
                skysum[NANs] = 0.
            # Check on specobjs
            if not specobjs[sl][o].check_trace(scitrace[sl]['traces'][:, o]):
                debugger.set_trace()
                msgs.error("Bad match to specobj in boxcar!")
            # Fill
            specobjs[sl][o].boxcar['wave'] = wvsum.copy()*u.AA  # Yes, units enter here
            specobjs[sl][o].boxcar['counts'] = scisum.copy()
            specobjs[sl][o].boxcar['var'] = varsum.copy()
            if np.sum(specobjs[sl][o].boxcar['var']) == 0.:
                debugger.set_trace()
            specobjs[sl][o].boxcar['sky'] = skysum.copy()  # per pixel
            specobjs[sl][o].boxcar['mask'] = boxmask.copy()
            # Find boxcar size
            slit_sz = []
            inslit = np.where(weight == 1.)
            for ii in range(weight.shape[0]):
                inrow = inslit[0] == ii
                if np.sum(inrow) > 0:
                    slit_sz.append(np.max(inslit[1][inrow])-np.min(inslit[1][inrow]))
            slit_pix = np.median(slit_sz)  # Pixels
            specobjs[sl][o].boxcar['size'] = slit_pix
    # Return
    return bgcorr


def obj_profiles(slf, det, specobjs, sciframe, varframe, skyframe, crmask,
                 scitrace, COUNT_LIM=25., doqa=True, pickle_file=None):
    """ Derive spatial profiles for each object
    Parameters
    ----------
    slf
    det
    specobjs
    sciframe
    varframe
    skyframe
    crmask
    scitrace
    Returns
    -------
    """
    '''  FOR DEVELOPING
    import pickle
    if False:
        tilts = slf._tilts[det-1]
        args = [det, specobjs, sciframe, varframe, skyframe, crmask, scitrace, tilts]
        msgs.warn("Pickling in the profile code")
        with open("trc_pickle.p",'wb') as f:
            pickle.dump(args,f)
        debugger.set_trace()
    if pickle_file is not None:
        f = open(pickle_file,'r')
        args = pickle.load(f)
        f.close()
        det, specobjs, sciframe, varframe, skyframe, crmask, scitrace, tilts = args
        slf = None
    else:
        tilts = slf._tilts[det-1]
    '''
    # Init QA
    #
    sigframe = np.sqrt(varframe)
    # Loop on slits
    for sl in range(len(specobjs)):
        # Loop on objects
        nobj = scitrace[sl]['traces'].shape[1]
        scitrace[sl]['opt_profile'] = []
        msgs.work("Should probably loop on S/N")
        for o in range(nobj):
            msgs.info("Deriving spatial profile of object {0:d}/{1:d} in slit {2:d}/{3:d}".format(o+1, nobj, sl+1, len(specobjs)))
            # Get object pixels
            if scitrace[sl]['background'] is None:
                # The object for all slits is provided in the first extension
                objreg = np.copy(scitrace[0]['object'][:, :, o])
                wzro = np.where(slf._slitpix[det - 1] != sl + 1)
                objreg[wzro] = 0.0
            else:
                objreg = scitrace[sl]['object'][:, :, o]
            # Calculate slit image
            slit_img = artrace.slit_image(slf, det, scitrace[sl], o)#, tilts=tilts)
            # Object pixels
            weight = objreg.copy()
            # Identify good rows
            gdrow = np.where(specobjs[sl][o].boxcar['counts'] > COUNT_LIM)[0]
            # Normalized image
            norm_img = sciframe / np.outer(specobjs[sl][o].boxcar['counts'], np.ones(sciframe.shape[1]))
            # Eliminate rows with CRs (wipes out boxcar)
            crspec = np.sum(crmask*weight, axis=1)
            cr_rows = np.where(crspec > 0)[0]
            weight[cr_rows, :] = 0.
            #
            if len(gdrow) > 100:  # Good S/N regime
                msgs.info("Good S/N for profile")
                # Eliminate low count regions
                badrow = np.where(specobjs[sl][o].boxcar['counts'] < COUNT_LIM)[0]
                weight[badrow, :] = 0.
                # Extract profile
                gdprof = (weight > 0) & (sigframe > 0.)
                slit_val = slit_img[gdprof]
                flux_val = norm_img[gdprof]
                #weight_val = sciframe[gdprof]/sigframe[gdprof]  # S/N
                weight_val = 1./sigframe[gdprof]  # 1/N
                msgs.work("Weight by S/N in boxcar extraction? [avoid CRs; smooth?]")
                # Fit
                fdict = dict(func=settings.argflag['science']['extraction']['profile'], deg=3)
                if fdict['func'] == 'gaussian':
                    fdict['deg'] = 2
                elif fdict['func'] == 'moffat':
                    fdict['deg'] = 3
                else:
                    msgs.error("Not ready for this type of object profile")
                msgs.work("Might give our own guess here instead of using default")
                guess = None
                try:
                    mask, gfit = arutils.robust_polyfit(slit_val, flux_val, fdict['deg'], function=fdict['func'], weights=weight_val, maxone=False, guesses=guess)
                except RuntimeError:
                    msgs.warn("Bad Profile fit for object={:s}.  Skipping Optimal".format(specobjs[sl][o].idx))
                    scitrace[sl]['opt_profile'].append(fdict)
                    continue
                except ValueError:
                    debugger.set_trace()  # NaNs in the values?  Check
                msgs.work("Consider flagging/removing CRs here")
                # Record
                fdict['param'] = gfit.copy()
                fdict['mask'] = mask
                fdict['slit_val'] = slit_val
                fdict['flux_val'] = flux_val
                scitrace[sl]['opt_profile'].append(copy.deepcopy(fdict))
                specobjs[sl][o].optimal['fwhm'] = fdict['param'][1]  # Pixels
                if msgs._debug['obj_profile']:
                    gdp = mask == 0
                    mn = np.min(slit_val[gdp])
                    mx = np.max(slit_val[gdp])
                    xval = np.linspace(mn, mx, 1000)
                    model = arutils.func_val(gfit, xval, fdict['func'])
                    import matplotlib.pyplot as plt
                    plt.clf()
                    ax = plt.gca()
                    ax.scatter(slit_val[gdp], flux_val[gdp], marker='.', s=0.7, edgecolor='none', facecolor='black')
                    ax.plot(xval, model, 'b')
                    # Gaussian too?
                    if False:
                        fdictg = dict(func='gaussian', deg=2)
                        maskg, gfitg = arutils.robust_polyfit(slit_val, flux_val, fdict['deg'], function=fdictg['func'], weights=weight_val, maxone=False)
                        modelg = arutils.func_val(gfitg, xval, fdictg['func'])
                        ax.plot(xval, modelg, 'r')
                    plt.show()
                    debugger.set_trace()
            elif len(gdrow) > 10:  #
                msgs.warn("Low extracted flux for obj={:s} in slit {:d}.  Not ready for Optimal".format(specobjs[sl][o].idx,sl+1))
                scitrace[sl]['opt_profile'].append({})
                continue
    # QA
    if not msgs._debug['no_qa'] and doqa:
        msgs.info("Preparing QA for spatial object profiles")
        arqa.obj_profile_qa(slf, specobjs, scitrace)
    return


def optimal_extract(slf, det, specobjs, sciframe, varframe,
                    skyframe, crmask, scitrace,
                    pickle_file=None, profiles=None):
    """ Preform optimal extraction
    Standard Horne approach

    Parameters
    ----------
    slf
    det
    specobjs
    sciframe
    varframe
    crmask
    scitrace
    COUNT_LIM
    pickle_file

    Returns
    -------
    newvar : ndarray
      Updated variance array that includes object model
    """
    from pypit import arproc
    # Setup
    #rnimg = arproc.rn_frame(slf,det)
    #model_var = np.abs(skyframe + sciframe - np.sqrt(2)*rnimg + rnimg**2)  # sqrt 2 term deals with negative flux/sky
    #model_ivar = 1./model_var
    # Inverse variance
    model_ivar = np.zeros_like(varframe)
    gdvar = varframe > 0.
    model_ivar[gdvar] = arutils.calc_ivar(varframe[gdvar])
    cr_mask = 1.0-crmask
    # Object model image
    obj_model = np.zeros_like(varframe)
    # Loop on slits
    for sl in range(len(specobjs)):
        # Loop on objects
        nobj = scitrace[sl]['traces'].shape[1]
        for o in range(nobj):
            msgs.info("Performing optimal extraction of object {0:d}/{1:d} in slit {2:d}/{3:d}".format(o+1, nobj, sl+1, len(specobjs)))
            # Get object pixels
            if scitrace[sl]['background'] is None:
                # The object for all slits is provided in the first extension
                objreg = np.copy(scitrace[0]['object'][:, :, o])
                wzro = np.where(slf._slitpix[det - 1] != sl + 1)
                objreg[wzro] = 0.0
            else:
                objreg = scitrace[sl]['object'][:, :, o]
            # Fit dict
            fit_dict = scitrace[sl]['opt_profile'][o]
            if 'param' not in fit_dict.keys():
                continue
            # Slit image
            slit_img = artrace.slit_image(slf, det, scitrace[sl], o)#, tilts=tilts)
            #msgs.warn("Turn off tilts")
            # Object pixels
            weight = objreg.copy()
            gdo = (weight > 0) & (model_ivar > 0)
            # Profile image
            prof_img = np.zeros_like(weight)
            prof_img[gdo] = arutils.func_val(fit_dict['param'], slit_img[gdo],
                                             fit_dict['func'])
            # Normalize
            norm_prof = np.sum(prof_img, axis=1)
            prof_img /= np.outer(norm_prof + (norm_prof == 0.), np.ones(prof_img.shape[1]))
            # Mask (1=good)
            mask = np.zeros_like(prof_img)
            mask[gdo] = 1.
            mask *= cr_mask

            # Optimal flux
            opt_num = np.sum(mask * sciframe * model_ivar * prof_img, axis=1)
            opt_den = np.sum(mask * model_ivar * prof_img**2, axis=1)
            opt_flux = opt_num / (opt_den + (opt_den == 0.))
            # Optimal wave
            opt_num = np.sum(slf._mswave[det-1] * model_ivar * prof_img**2, axis=1)
            opt_den = np.sum(model_ivar * prof_img**2, axis=1)
            opt_wave = opt_num / (opt_den + (opt_den == 0.))
            if (np.sum(opt_wave < 1.) > 0) and settings.argflag["reduce"]["calibrate"]["wavelength"] != "pixel":
                debugger.set_trace()
                msgs.error("Zero value in wavelength array. Uh-oh")
            # Optimal ivar
            opt_num = np.sum(mask * model_ivar * prof_img**2, axis=1)
            ivar_den = np.sum(mask * prof_img, axis=1)
            opt_ivar = opt_num * arutils.calc_ivar(ivar_den)

            # Save
            specobjs[sl][o].optimal['wave'] = opt_wave.copy()*u.AA  # Yes, units enter here
            specobjs[sl][o].optimal['counts'] = opt_flux.copy()
            gdiv = (opt_ivar > 0.) & (ivar_den > 0.)
            opt_var = np.zeros_like(opt_ivar)
            opt_var[gdiv] = arutils.calc_ivar(opt_ivar[gdiv])
            specobjs[sl][o].optimal['var'] = opt_var.copy()
            #specobjs[o].boxcar['sky'] = skysum  # per pixel

            # Update object model
            counts_image = np.outer(opt_flux, np.ones(prof_img.shape[1]))
            obj_model += prof_img * counts_image
            '''
            if 'OPTIMAL' in msgs._debug:
                debugger.set_trace()
                debugger.xplot(opt_wave, opt_flux, np.sqrt(opt_var))
            '''
    # Generate new variance image
    newvar = arproc.variance_frame(slf, det, sciframe, -1,
                                   skyframe=skyframe, objframe=obj_model)
    # Return
    return newvar


def boxcar_cen(slf, det, img):
    """ Simple boxcar down center of the slit

    Parameters
    ----------
    slf
    det
    img

    Returns
    -------
    spec : ndarray

    """
    # Extract a slit down the center (as in ararc, or should be!)
    ordcen = slf.GetFrame(slf._pixcen, det)
    op1 = ordcen+1
    op2 = ordcen+2
    om1 = ordcen-1
    om2 = ordcen-2
    # Extract
    censpec = (img[:,ordcen]+img[:,op1]+img[:,op2]+img[:,om1]+img[:,om2])/5.0
    if len(censpec.shape) == 3:
        censpec = censpec[:, 0].flatten()
    # Return
    return censpec
