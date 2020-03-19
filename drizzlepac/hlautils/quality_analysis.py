"""Code that evaluates the quality of products generated by the drizzlepac package.

The JSON files generated here can be converted directly into a Pandas DataFrame
using the syntax:

>>> import json
>>> import pandas as pd
>>> with open("<rootname>_astrometry_resids.json") as jfile:
>>>     resids = json.load(jfile)
>>> pdtab = pd.DataFrame(resids)

These DataFrames can then be concatenated using:

>>> allpd = pdtab.concat([pdtab2, pdtab3])

where 'pdtab2' and 'pdtab3' are DataFrames generated from other datasets.  For
more information on how to merge DataFrames, see 

https://pandas.pydata.org/pandas-docs/stable/user_guide/merging.html

Visualization of these Pandas DataFrames with Bokeh can follow the example
from:

https://programminghistorian.org/en/lessons/visualizing-with-bokeh


"""
import json
import os

from astropy.io import fits
from astropy.stats import sigma_clipped_stats
import numpy as np

from stsci.tools.fileutil import countExtn
import tweakwcs

from . import astrometric_utils as amutils
from .. import tweakutils


def determine_alignment_residuals(input, files, max_srcs=2000):
    """Determine the relative alignment between members of an association.

    Parameters
    -----------
    input : string
        Original pipeline input filename.  This filename will be used to
        define the output analysis results filename.

    files : list
        Set of files on which to actually perform comparison.  The original
        pipeline can work on both CTE-corrected and non-CTE-corrected files,
        but this comparison will only be performed on CTE-corrected
        products when available.

    Returns
    --------
    results : string
        Name of JSON file containing all the extracted results from the comparisons
        being performed.
    """
    # Open all files as HDUList objects
    hdus = [fits.open(f) for f in files]
    # Determine sources from each chip
    src_cats = []
    num_srcs = []
    for hdu in hdus:
        numsci = countExtn(hdu)
        nums = 0
        img_cats = {}
        for chip in range(numsci):
            chip += 1
            img_cats[chip] = amutils.extract_point_sources(hdu[("SCI", chip)].data, high_sn=max_srcs)
            nums += len(img_cats[chip])
        num_srcs.append(nums)
        src_cats.append(img_cats)

    if max(num_srcs) <= 3:
        return None

    # src_cats = [amutils.generate_source_catalog(hdu) for hdu in hdus]
    # Combine WCS from HDULists and source catalogs into tweakwcs-compatible input
    imglist = []
    for i, (f, cat) in enumerate(zip(files, src_cats)):
        imglist += amutils.build_wcscat(f, i, cat)

    # Setup matching algorithm using parameters tuned to well-aligned images
    match = tweakwcs.TPMatch(searchrad=5, separation=1.0,
                             tolerance=4.0, use2dhist=True)
    try:
        # perform relative fitting
        matchlist = tweakwcs.align_wcs(imglist, None, match=match, expand_refcat=False)
        del matchlist
    except Exception:
        return None
    # Check to see whether there were any successful fits...
    align_success = False
    for img in imglist:
        if img.meta['fit_info']['status'] == 'SUCCESS':
            align_success = True
            break
    if align_success:
        # extract results in the style of 'tweakreg'
        resids = extract_residuals(imglist)

        # Define name for output JSON file...
        resids_file = "{}_astrometry_resids.json".format(input[:9])
        # Remove any previously computed results
        if os.path.exists(resids_file):
            os.remove(resids_file)
        # Dump the results to a JSON file now...
        with open(resids_file, 'w') as jfile:
            json.dump(resids, jfile)
    else:
        resids_file = None

    return resids_file

def extract_residuals(imglist):
    """Convert fit results and catalogs from tweakwcs into list of residuals"""
    group_dict = {}

    ref_ra, ref_dec = [], []
    for chip in imglist:
        group_id = chip.meta['group_id']
        group_name = chip.meta['filename']
        fitinfo = chip.meta['fit_info']
        if group_id not in group_dict:
            group_dict[group_name] = {'group_id': group_id, 'type': None,
                         'x': [], 'y': [], 
                         'ref_x': [], 'ref_y':[],
                         'rms_x': None, 'rms_y': None}
            cum_indx = 0

        if fitinfo['status'] == 'REFERENCE':
            group_dict[group_name]['type'] = 'REFERENCE'
            rra, rdec = chip.det_to_world(chip.meta['catalog']['x'],
                                          chip.meta['catalog']['y'])
            ref_ra = np.concatenate([ref_ra, rra])
            ref_dec = np.concatenate([ref_dec, rdec])
            continue

        img_mask = fitinfo['fitmask']
        ref_indx = fitinfo['matched_ref_idx'][img_mask]
        img_indx = fitinfo['matched_input_idx'][img_mask]
        # Extract X, Y for sources image being updated
        img_x, img_y, max_indx, chip_mask = get_tangent_positions(chip, img_indx,
                                                       start_indx=cum_indx)
        cum_indx += max_indx
        # Extract X, Y for sources from reference image
        ref_x, ref_y = chip.world_to_tanp(ref_ra[ref_indx][chip_mask], ref_dec[ref_indx][chip_mask])

        # store results in dict
        group_dict[group_name]['type'] = 'IMAGE'
        group_dict[group_name].update(
             {'xsh': fitinfo['shift'][0], 'ysh': fitinfo['shift'][1],
             'rot': fitinfo['<rot>'], 'scale': fitinfo['<scale>'],
             'nmatches': fitinfo['nmatches'], 'skew': fitinfo['skew']})

        group_dict[group_name]['x'].extend(img_x)
        group_dict[group_name]['y'].extend(img_y)
        group_dict[group_name]['ref_x'].extend(ref_x)
        group_dict[group_name]['ref_y'].extend(ref_y)        
        group_dict[group_name]['rms_x'] = sigma_clipped_stats((img_x - ref_x))[-1]
        group_dict[group_name]['rms_y'] = sigma_clipped_stats((img_y - ref_y))[-1]


    return group_dict

def get_tangent_positions(chip, indices, start_indx=0):
    img_x = []
    img_y = []
    fitinfo = chip.meta['fit_info']
    img_ra = fitinfo['fit_RA']
    img_dec = fitinfo['fit_DEC']

    # Extract X, Y for sources image being updated
    max_indx = len(chip.meta['catalog'])
    chip_indx = np.where(np.logical_and(indices >= start_indx,
                                        indices < max_indx + start_indx))[0]
    # Get X,Y position in tangent plane where fit was done
    chip_x, chip_y = chip.world_to_tanp(img_ra[chip_indx], img_dec[chip_indx])
    img_x.extend(chip_x)
    img_y.extend(chip_y)

    return img_x, img_y, max_indx, chip_indx


# -------------------------------------------------------------------------------
# Simple interface for running all the analysis functions defined for this package
def run_all(input, files):

    json_file = determine_alignment_residuals(input, files)

    return json_file


# -------------------------------------------------------------------------------
#  Code for generating relevant plots from these results
def generate_plots(json_data):
    """Create plots from json file or json data"""
    
    if isinstance(json_data, str):
        # Open json file and read in data
        with open(json_data) as jfile:
            json_data = json.load(jfile)
            
    fig_id = 0
    for fname in json_data:
        data = json_data[fname]
        if data['type'] == 'REFERENCE':
            continue
        rootname = fname.split("_")[0]
        coldata = [data['x'], data['y'], data['ref_x'], data['ref_y']]
        # Insure all columns are numpy arrays
        coldata = [np.array(c) for c in coldata]
        title_str = 'Residuals\ for\ {0}\ using\ {1:6d}\ sources'.format(
                    fname.replace('_','\_'),data['nmatches'])
        
        vector_name = '{}_vector_quality.png'.format(rootname)
        resids_name = '{}_resids_quality.png'.format(rootname)
        # Generate plots
        tweakutils.make_vector_plot(None, data=coldata,
                     figure_id=fig_id, title=title_str, vector=True,
                     plotname=vector_name)
        fig_id += 1
        tweakutils.make_vector_plot(None, data=coldata, ylimit=0.5,
                     figure_id=fig_id, title=title_str, vector=False,
                     plotname=resids_name)
        fig_id += 1


                     

    
    
    
