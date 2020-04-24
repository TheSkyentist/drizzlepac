"""Code that evaluates the quality of the SVM products generated by the drizzlepac package.

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

# Standard library imports
import collections
import json
import os
import pdb
import sys

# Related third party imports
from astropy.coordinates import SkyCoord
from astropy.io import ascii, fits
from astropy.stats import sigma_clipped_stats
from astropy.table import Table
import numpy as np
from scipy.spatial import KDTree

# Local application imports
from drizzlepac.hlautils import astrometric_utils as au
import drizzlepac.hlautils.diagnostic_utils as du
import drizzlepac.devutils.comparison_tools.compare_sourcelists as csl
from stsci.tools import logutil
from stwcs import wcsutil
from stwcs.wcsutil import HSTWCS

__taskname__ = 'svm_quality_analysis'

MSG_DATEFMT = '%Y%j%H%M%S'
SPLUNK_MSG_FORMAT = '%(asctime)s %(levelname)s src=%(name)s- %(message)s'
log = logutil.create_logger(__name__, level=logutil.logging.NOTSET, stream=sys.stdout,
                            format=SPLUNK_MSG_FORMAT, datefmt=MSG_DATEFMT)
# ----------------------------------------------------------------------------------------------------------------------

def characterize_gaia_distribution(hap_obj, log_level=logutil.logging.NOTSET):
    """Statistically describe distribution of GAIA sources in footprint.

    Computes and writes the file to a json file:

    - Number of GAIA sources
    - X centroid location
    - Y centroid location
    - X offset of centroid from image center
    - Y offset of centroid from image center
    - X standard deviation
    - Y standard deviation
    - minimum closest neighbor distance
    - maximum closest neighbor distance
    - mean closest neighbor distance
    - standard deviation of closest neighbor distances

    Parameters
    ----------
    hap_obj : drizzlepac.hlautils.Product.FilterProduct
        hap product object to process

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the .log file.
        Default value is 'NOTSET'.

    Returns
    -------
    Nothing
    """
    log.setLevel(log_level)

    # get table of GAIA sources in footprint
    gaia_table = generate_gaia_catalog(hap_obj, columns_to_remove=['mag', 'objID', 'GaiaID'])

    # if log_level is either 'DEBUG' or 'NOTSET', write out GAIA sources to DS9 region file
    if log_level <= logutil.logging.DEBUG:
        reg_file = "{}_gaia_sources.reg".format(hap_obj.drizzle_filename[:-9])
        gaia_table.write(reg_file, format='ascii.csv')
        log.debug("Wrote GAIA source RA and Dec positions to DS9 region file '{}'".format(reg_file))

    # convert RA, Dec to image X, Y
    outwcs = HSTWCS(hap_obj.drizzle_filename + "[1]")
    x, y = outwcs.all_world2pix(gaia_table['RA'], gaia_table['DEC'], 1)

    # compute stats for the distribution
    centroid = [np.mean(x), np.mean(y)]
    centroid_offset = []
    for idx in range(0, 2):
        centroid_offset.append(outwcs.wcs.crpix[idx] - centroid[idx])
    std_dev = [np.std(x), np.std(y)]

    # Find straight-line distance to the closest neighbor for each GAIA source
    xys = np.array([x, y])
    xys = xys.reshape(len(x), 2)
    tree = KDTree(xys)
    neighborhood = tree.query(xys, 2)
    min_seps = np.empty([0])
    for sep_pair in neighborhood[0]:
        min_seps = np.append(min_seps, sep_pair[1])

    # add statistics to out_dict
    out_dict = collections.OrderedDict()
    out_dict["units"] = "pixels"
    out_dict["Number of GAIA sources"] = len(gaia_table)
    axis_list = ["X", "Y"]
    title_list = ["centroid", "offset of centroid from image center", "standard deviation"]
    for item_value, item_title in zip([centroid, centroid_offset, std_dev], title_list):
        for axis_item in enumerate(axis_list):
            log.info("{} {} ({}): {}".format(axis_item[1], item_title, out_dict["units"], item_value[axis_item[0]]))
            out_dict["{} {}".format(axis_item[1], item_title)] = item_value[axis_item[0]]
    min_sep_stats = [min_seps.min(), min_seps.max(), min_seps.mean(), min_seps.std()]
    min_sep_title_list = ["minimum closest neighbor distance",
                          "maximum closest neighbor distance",
                          "mean closest neighbor distance",
                          "standard deviation of closest neighbor distances"]
    for item_value, item_title in zip(min_sep_stats, min_sep_title_list):
        log.info("{} ({}): {}".format(item_title, out_dict["units"], item_value))
        out_dict[item_title] = item_value

    # write catalog to HapDiagnostic-formatted .json file.
    diag_obj = du.HapDiagnostic(log_level=log_level)
    diag_obj.instantiate_from_hap_obj(hap_obj,
                                      data_source="{}.characterize_gaia_distribution".format(__taskname__),
                                      description="A statistical characterization of the distribution of GAIA sources in image footprint")
    diag_obj.add_data_item(out_dict, "distribution characterization statistics")
    diag_obj.write_json_file(hap_obj.drizzle_filename[:-9] + "_svm_gaia_distribution_characterization.json", clobber=True)


# ----------------------------------------------------------------------------------------------------------------------

def compare_num_sources(catalog_list, drizzle_list, log_level=logutil.logging.NOTSET):
    """Determine the number of viable sources actually listed in SVM output catalogs.

    Parameters
    ----------
    catalog_list: list of strings
        Set of files on which to actually perform comparison.  Catalogs, Point and
        Segment, are generated for all of the Total data products in a single visit.
        The catalogs are detector-dependent.

    drizzle_list: list of strings
        Drizzle files for the Total products which were mined to generate the output catalogs.

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the .log file.
        Default value is 'NOTSET'.

    .. note:: This routine can be run either as a direct call from the hapsequencer.py routine,
    or it can invoked by a simple Python driver (or from within a Python session) by providing
    the names of the previously computed files as lists. The files must exist in the working directory.
    """
    log.setLevel(log_level)

    pnt_suffix = '_point-cat.ecsv'
    seg_suffix = '_segment-cat.ecsv'

    # Generate a separate JSON file for each detector
    # Drizzle filename example: hst_11665_06_wfc3_ir_total_ib4606_drz.fits
    # The filename is all lower-case by design.
    for drizzle_file in drizzle_list:
        tokens = drizzle_file.split('_')
        detector = tokens[4]
        ipppss = tokens[6]

        sources_dict = {'detector': detector, 'point': 0, 'segment': 0}

        # Construct the output JSON filename
        json_filename = ipppss + '_' + detector + '_svm_num_sources.json'

        # Construct catalog names for catalogs that should have been produced
        prefix = '_'.join(tokens[0:-1])
        cat_names = [prefix + pnt_suffix, prefix + seg_suffix]

        # If the catalog were actually produced, get the number of sources.
        # A catalog may not be produced because it was not requested, or there
        # was an error.  However, for the purposes of this program, it is OK
        # that no catalog was produced.
        for catalog in cat_names:
            does_exist = any(catalog in x for x in catalog_list)

            # if the catalog exists, open it and find the number of sources string
            num_sources = -1
            cat_type = ""
            if does_exist:
                file = open(catalog, 'r')
                for line in file:
                    sline = line.strip()

                    # All the comments are grouped at the start of the file. When
                    # the first non-comment line is found, there is no need to look further.
                    if not sline.startswith('#'):
                        log.info("Number of sources not reported in Catalog: {}.".format(catalog))
                        break

                    # When the matching comment line is found, get the value.
                    if sline.find('Number of sources') != -1:
                        num_sources = sline.split(' ')[-1][0:-1]
                        log.info("Catalog: {} Number of sources: {}.".format(catalog, num_sources))
                        break

                cat_type = 'point' if catalog.find("point") != -1 else 'segment'
                sources_dict[cat_type] = int(num_sources)

        # Set up the diagnostic object and write out the results
        diagnostic_obj = du.HapDiagnostic()
        diagnostic_obj.instantiate_from_fitsfile(drizzle_file,
                                                 data_source="{}.compare_num_sources".format(__taskname__),
                                                 description="Number of sources in Point and Segment catalogs")
        diagnostic_obj.add_data_item(sources_dict, 'number_of_sources')
        diagnostic_obj.write_json_file(json_filename)
        log.info("Generated quality statistics (number of sources) as {}.".format(json_filename))

        # Clean up
        del diagnostic_obj

# ------------------------------------------------------------------------------------------------------------

def compare_ra_dec_crossmatches(hap_obj, log_level=logutil.logging.NOTSET):
    """Compare the equatorial coordinates of cross-matches sources between the Point and Segment catalogs.
    The results .json file contains the following information:

        - image header information
        - cross-match details (input catalog lengths, number of cross-matched sources, coordinate system)
        - catalog containing RA and dec values of cross-matched point catalog sources
        - catalog containing RA and dec values of cross-matched segment catalog sources
        - Statistics describing the on-sky seperation of the cross-matched point and segment catalogs
        (non-clipped and sigma-clipped mean, median and standard deviation values)

    Parameters
    ----------
    hap_obj : drizzlepac.hlautils.Product.FilterProduct
        hap filter product object to process

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the .log file.
        Default value is 'NOTSET'.

    Returns
    --------
    nothing.
    """
    log.setLevel(log_level)
    slNames = [hap_obj.point_cat_filename,hap_obj.segment_cat_filename]
    imgNames = [hap_obj.drizzle_filename, hap_obj.drizzle_filename]
    good_flag_sum = 255 # all bits good

    diag_obj = du.HapDiagnostic(log_level=log_level)
    diag_obj.instantiate_from_hap_obj(hap_obj,
                                      data_source="{}.compare_ra_dec_crossmatches".format(__taskname__),
                                      description="matched point and segment catalog RA and Dec values")
    json_results_dict = collections.OrderedDict()
    # add reference and comparision catalog filenames as header elements
    json_results_dict["point catalog filename"] = slNames[0]
    json_results_dict["segment catalog filename"] = slNames[1]

    # 1: Read in sourcelists files into astropy table or 2-d array so that individual columns from each sourcelist can be easily accessed later in the code.
    point_data, seg_data = csl.slFiles2dataTables(slNames)
    log.info("Valid point data columns:   {}".format(list(point_data.keys())))
    log.info("Valid segment data columns: {}".format(list(seg_data.keys())))
    log.info("\n")
    log.info("Data columns to be compared:")
    columns_to_compare = list(set(point_data.keys()).intersection(set(seg_data.keys())))
    for listItem in sorted(columns_to_compare):
        log.info(listItem)
    log.info("\n")
    # 2: Run starmatch_hist to get list of matched sources common to both input sourcelists
    slLengths = [len(point_data['RA']), len(seg_data['RA'])]
    json_results_dict['point catalog length'] = slLengths[0]
    json_results_dict['segment catalog length'] = slLengths[1]
    matching_lines_ref, matching_lines_img = csl.getMatchedLists(slNames, imgNames, slLengths, log_level=log_level)
    json_results_dict['number of cross-matches'] = len(matching_lines_ref)

    # Report number and percentage of the total number of detected ref and comp sources that were matched
    log.info("Cross-matching results")
    log.info(
        "Point sourcelist:  {} of {} total sources cross-matched ({}%)".format(len(matching_lines_ref), slLengths[0],
                                                                               100.0 * (float(
                                                                                   len(matching_lines_ref)) / float(
                                                                                   slLengths[0]))))
    log.info(
        "Segment sourcelist: {} of {} total sources cross-matched ({}%)".format(len(matching_lines_img), slLengths[1],
                                                                                100.0 * (float(
                                                                                    len(matching_lines_img)) / float(
                                                                                    slLengths[1]))))
    # return without creating a .json if no cross-matches are found
    if len(matching_lines_ref) == 0 or len(matching_lines_img) == 0:
        log.error("*** No matching sources were found. Comparisons cannot be computed. No json file will be produced.***")
        return
    # 2: Create masks to remove missing values or values not considered "good" according to user-specified good bit values
    # 2a: create mask that identifies lines any value from any column is missing
    missing_mask = csl.mask_missing_values(point_data, seg_data, matching_lines_ref, matching_lines_img, columns_to_compare)
    # 2b: create mask based on flag values
    matched_values = csl.extractMatchedLines("FLAGS", point_data, seg_data, matching_lines_ref, matching_lines_img)
    bitmask = csl.make_flag_mask(matched_values, good_flag_sum, missing_mask)

    matched_values_ra = csl.extractMatchedLines("RA", point_data, seg_data, matching_lines_ref, matching_lines_img,
                                                bitmask=bitmask)
    matched_values_dec = csl.extractMatchedLines("DEC", point_data, seg_data, matching_lines_ref, matching_lines_img,
                                                 bitmask=bitmask)

    if matched_values_ra.shape[1] > 0 and matched_values_ra.shape[1] == matched_values_dec.shape[1]:
        # get coordinate system type from fits headers

        point_frame = fits.getval(imgNames[0], "radesys", ext=('sci', 1)).lower()
        seg_frame = fits.getval(imgNames[1], "radesys", ext=('sci', 1)).lower()
        # Add 'ref_frame' and 'comp_frame" values to header so that will SkyCoord() execute OK
        json_results_dict["point frame"] = point_frame
        json_results_dict["segment frame"] = seg_frame

        # convert reference and comparision RA/Dec values into SkyCoord objects
        matched_values_point = SkyCoord(matched_values_ra[0, :], matched_values_dec[0, :], frame=point_frame,
                                        unit="deg")
        matched_values_seg = SkyCoord(matched_values_ra[1, :], matched_values_dec[1, :], frame=seg_frame,
                                      unit="deg")
        # convert to ICRS coord system
        if point_frame != "icrs":
            matched_values_point = matched_values_point.icrs
        if seg_frame != "icrs":
            matched_values_seg = matched_values_seg.icrs

        # compute on-sky separations in arcseconds
        sep = matched_values_seg.separation(matched_values_point).arcsec

        # Compute and store statistics  on separations
        sep_stat_dict=collections.OrderedDict()
        sep_stat_dict["units"] = "arcseconds"
        sep_stat_dict["Non-clipped min"] = np.min(sep)
        sep_stat_dict["Non-clipped max"] = np.max(sep)
        sep_stat_dict["Non-clipped mean"] = np.mean(sep)
        sep_stat_dict["Non-clipped median"] = np.median(sep)
        sep_stat_dict["Non-clipped standard deviation"] = np.std(sep)
        sigma = 3
        maxiters = 3
        clippedStats = sigma_clipped_stats(sep, sigma=sigma, maxiters=maxiters)
        sep_stat_dict["{}x{} sigma-clipped mean".format(maxiters, sigma)] = clippedStats[0]
        sep_stat_dict["{}x{} sigma-clipped median".format(maxiters, sigma)] = clippedStats[1]
        sep_stat_dict["{}x{} sigma-clipped standard deviation".format(maxiters, sigma)] = clippedStats[2]

        # Create output catalogs for json file
        out_cat_point = Table([matched_values_ra[0], matched_values_dec[0]], names=("Right ascension", "Declination"))
        out_cat_seg = Table([matched_values_ra[1], matched_values_dec[1]], names=("Right ascension", "Declination"))
        for table_item in [out_cat_point,out_cat_seg]:
            for col_name in ["Right ascension", "Declination"]:
                table_item[col_name].unit = "degrees"  # Add correct units

        # add various data items to diag_obj
        diag_obj.add_data_item(json_results_dict, "Cross-match details")
        diag_obj.add_data_item(out_cat_point, "Cross-matched point catalog")
        diag_obj.add_data_item(out_cat_seg, "Cross-matched segment catalog")
        diag_obj.add_data_item(sep_stat_dict, "Segment - point on-sky separation statistics")

        # write everything out to the json file
        json_filename = hap_obj.drizzle_filename[:-9]+"_svm_point_segment_crossmatch.json"
        diag_obj.write_json_file(json_filename, clobber=True)
    else:
        log.warning("Point vs. segment catalog cross match test could not be performed.")

# ------------------------------------------------------------------------------------------------------------


def find_gaia_sources(hap_obj, log_level=logutil.logging.NOTSET):
    """Creates a catalog of all GAIA sources in the footprint of a specified HAP final product image, and
    stores the GAIA object catalog as a hap diagnostic json file. The catalog contains RA, Dec and magnitude
    of each identified source. The catalog is sorted in decending order by brightness.

    Parameters
    ----------
    hap_obj : drizzlepac.hlautils.Product.TotalProduct, drizzlepac.hlautils.Product.FilterProduct, or
        drizzlepac.hlautils.Product.ExposureProduct, depending on input.
        hap product object to process

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the .log file.
        Default value is 'NOTSET'.

    Returns
    -------
    Nothing.
    """
    log.setLevel(log_level)
    gaia_table = generate_gaia_catalog(hap_obj, columns_to_remove=['objID', 'GaiaID'])
    # write catalog to HapDiagnostic-formatted .json file.
    diag_obj = du.HapDiagnostic(log_level=log_level)
    diag_obj.instantiate_from_hap_obj(hap_obj,
                                      data_source="{}.find_gaia_sources".format(__taskname__),
                                      description="A table of GAIA sources in image footprint")
    diag_obj.add_data_item(gaia_table, "GAIA sources")  # write catalog of identified GAIA sources
    diag_obj.add_data_item(len(gaia_table), "Number of GAIA sources")  # write the number of identified GAIA sources
    diag_obj.write_json_file(hap_obj.drizzle_filename[:-9]+"_svm_gaia_sources.json", clobber=True)

    # Clean up
    del diag_obj
    del gaia_table

# ----------------------------------------------------------------------------------------------------------------------

def generate_gaia_catalog(hap_obj, columns_to_remove = None):
    """Uses astrometric_utils.create_astrometric_catalog() to create a catalog of all GAIA sources in the
    image footprint. This catalog contains right ascension, declination, and magnitude values, and is sorted
    in descending order by brightness.

    Parameters
    ----------
    hap_obj : drizzlepac.hlautils.Product.TotalProduct, drizzlepac.hlautils.Product.FilterProduct, or
        drizzlepac.hlautils.Product.ExposureProduct, depending on input.
        hap product object to process

    Returns
    -------
    gaia_table : astropy table
        table containing right ascension, declination, and magnitude of all GAIA sources identified in the
        image footprint, sorted in descending order by brightness.
    """
    # Gather list of input flc/flt images
    img_list = []
    log.debug("GAIA catalog will be created using the following input images:")
    # Create a list of the input flc.fits/flt.fits that were drizzled to create the final HAP product being
    # processed here. edp_item.info and hap_obj.info are both structured as follows:
    # <proposal id>_<visit #>_<instrument>_<detector>_<input filename>_<filter>_<drizzled product
    # image filetype>
    # Example: '10265_01_acs_wfc_j92c01b9q_flc.fits_f606w_drc'
    # what is being extracted here is just the input filename, which in this case is 'j92c01b9q_flc.fits'.
    if hasattr(hap_obj, "edp_list"):  # for total and filter product objects
        for edp_item in hap_obj.edp_list:
            parse_info = edp_item.info.split("_")
            imgname = "{}_{}".format(parse_info[4], parse_info[5])
            log.debug(imgname)
            img_list.append(imgname)
    else:  # For single-exposure product objects
        parse_info = hap_obj.info.split("_")
        imgname = "{}_{}".format(parse_info[4], parse_info[5])
        log.debug(imgname)
        img_list.append(imgname)

    # generate catalog of GAIA sources
    gaia_table = au.create_astrometric_catalog(img_list, gaia_only=True, use_footprint=True)

    # trim off specified columns
    if columns_to_remove:
        gaia_table.remove_columns(columns_to_remove)

    # remove sources outside image footprint
    outwcs = wcsutil.HSTWCS(hap_obj.drizzle_filename, ext=1)
    x, y = outwcs.all_world2pix(gaia_table['RA'], gaia_table['DEC'], 1)
    imghdu = fits.open(hap_obj.drizzle_filename)
    in_img_data = imghdu['WHT'].data.copy()
    in_img_data = np.where(in_img_data == 0, np.nan, in_img_data)
    mask = au.within_footprint(in_img_data, outwcs, x, y)
    gaia_table = gaia_table[mask]

    # Report results to log
    if len(gaia_table) == 0:
        log.warning("No GAIA sources were found!")
    elif len(gaia_table) == 1:
        log.info("1 GAIA source was found.")
    else:
        log.info("{} GAIA sources were found.".format(len(gaia_table)))
    return gaia_table

# ----------------------------------------------------------------------------------------------------------------------


def compare_photometry(drizzle_list, log_level=logutil.logging.NOTSET):
    """Compare photometry measurements for sources cross matched between the Point and Segment catalogs.

    Parameters
    ----------
    drizzle_list: list of strings
        Drizzle files for the Filter products which were mined to generate the output catalogs.

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the .log file.
        Default value is 'NOTSET'.

    .. note:: This routine can be run either as a direct call from the hapsequencer.py routine,
    or it can invoked by a simple Python driver (or from within a Python session) by providing
    the names of the previously computed files as lists. The files must exist in the working directory.
    """
    log.setLevel(log_level)

    pnt_suffix = '_point-cat.ecsv'
    seg_suffix = '_segment-cat.ecsv'

    good_flag_sum = 255

    phot_column_names = ["MagAp1", "MagAp2"]
    error_column_names = ["MagErrAp1", "MagErrAp2"]

    # Generate a separate JSON file for each detector and filter product
    # Drizzle filename example: hst_11665_06_wfc3_ir_f110w_ib4606_drz.fits.
    # The "product" in this context is a filter name.
    # The filename is all lower-case by design.
    for drizzle_file in drizzle_list:
        tokens = drizzle_file.split('_')
        detector = tokens[4]
        filter_name = tokens[5]
        ipppss = tokens[6]

        # Set up the diagnostic object
        diagnostic_obj = du.HapDiagnostic()
        diagnostic_obj.instantiate_from_fitsfile(drizzle_file,
                                                 data_source="{}.compare_photometry".format(__taskname__),
                                                 description="Photometry differences in Point and Segment catalogs")
        summary_dict = {'detector': detector, 'filter_name': filter_name}

        # Construct the output JSON filename
        json_filename = '_'.join([ipppss, detector, 'svm', filter_name, 'photometry.json'])

        # Construct catalog names for catalogs that should have been produced
        # For any drizzled product, only two catalogs can be produced at most (point and segment).
        prefix = '_'.join(tokens[0:-1])
        cat_names = [prefix + pnt_suffix, prefix + seg_suffix]

        # Check that both catalogs exist
        for catalog in cat_names:
            does_exist = os.path.isfile(catalog)
            if not does_exist:
                log.warning("Catalog {} does not exist.  Both the Point and Segment catalogs must exist for comparison.".format(catalog))
                log.warning("Program skipping comparison of catalogs associated with {}.\n".format(drizzle_file))
                continue

        # If the catalogs were actually produced, then get the data.
        tab_point_measurements = ascii.read(cat_names[0])
        tab_seg_measurements = ascii.read(cat_names[1])

        # Unfortunately the Point and Segment catalogs use different names for the X and Y values
        # Point: ([X|Y]-Center)  Segment: ([X|Y]-Centroid. Reset the coordinate columns to be only X or Y.
        tab_point_measurements.rename_column('X-Center', 'X')
        tab_point_measurements.rename_column('Y-Center', 'Y')
        tab_seg_measurements.rename_column('X-Centroid', 'X')
        tab_seg_measurements.rename_column('Y-Centroid', 'Y')
        cat_lengths = [len(tab_point_measurements), len(tab_seg_measurements)]

        # Determine the column names common to both catalogs as a list
        common_columns = list(set(tab_point_measurements.colnames).intersection(set(tab_seg_measurements.colnames)))

        # Use the utilities in devutils to match the sources in the two lists - get
        # the indices of the matches.
        matches_point_to_seg, matches_seg_to_point = csl.getMatchedLists(cat_names,
                                                                         [drizzle_file,
                                                                         drizzle_file],
                                                                         cat_lengths,
                                                                         log_level=log_level)
        if len(matches_point_to_seg) == 0 or len(matches_seg_to_point) == 0:
            log.warning("Catalog {} and Catalog {} had no matching sources.".format(cat_names[0], cat_names[1]))
            log.warning("Program skipping comparison of catalogindexs associated with {}.\n".format(drizzle_file))
            continue

        # There are nan values present in the catalogs - create a mask which identifies these rows
        # which are missing valid data
        missing_values_mask = csl.mask_missing_values(tab_point_measurements, tab_seg_measurements,
                                                      matches_point_to_seg, matches_seg_to_point, common_columns)

        # Extract the Flag column from the two catalogs and get an ndarray (2, length)
        flag_matching = csl.extractMatchedLines('Flags', tab_point_measurements, tab_seg_measurements,
                                                matches_point_to_seg, matches_seg_to_point)

        # Generate a mask to accommodate the missing, as well as the "flagged" entries
        flag_values_mask = csl.make_flag_mask(flag_matching, good_flag_sum, missing_values_mask)

        # Extract the columns of interest from the two catalogs for each desired measurement
        # and get an ndarray (2, length)
        # array([[21.512, ..., 2.944], [21.6 , ..., 22.98]],
        #       [[21.872, ..., 2.844], [21.2 , ..., 22.8]])
        for index, phot_column_name in enumerate(phot_column_names):
            matching_phot_rows = csl.extractMatchedLines(phot_column_name, tab_point_measurements, tab_seg_measurements,
                                                         matches_point_to_seg, matches_seg_to_point, bitmask=flag_values_mask)

            # Compute the differences (Point - Segment)
            delta_phot = np.subtract(matching_phot_rows[0], matching_phot_rows[1])

            # Compute some basic statistics: mean difference and standard deviation, median difference,
            median_delta_phot = np.median(delta_phot)
            mean_delta_phot = np.mean(delta_phot)
            std_delta_phot = np.std(delta_phot)

            # NEED A BETTER WAY TO ASSOCIATE THE ERRORS WITH THE MEASUREMENTS
            # Compute the corresponding error of the differences
            matching_error_rows = csl.extractMatchedLines(error_column_names[index],
                                                          tab_point_measurements, tab_seg_measurements,
                                                          matches_point_to_seg, matches_seg_to_point,
                                                          bitmask=flag_values_mask)

            # Compute the error of the delta value (square root of the sum of the squares)
            result_error = np.sqrt(np.add(np.square(matching_error_rows[0]), np.square(matching_error_rows[1])))

            stat_key = 'Stats for Delta_' + phot_column_name + ' = Point_' + phot_column_name + ' - Segment_' + phot_column_name
            stat_dict = {stat_key: {'Mean Difference': mean_delta_phot, 'Standard Deviation': std_delta_phot,
                         'Median Difference': median_delta_phot}}
            summary_dict.update(stat_dict)

            # Write out the results
            diagnostic_obj.add_data_item(summary_dict, 'High-level Photometry Statistics on differences of Point - Segment')

        diagnostic_obj.write_json_file(json_filename)
        log.info("Generated photometry comparison for Point - Segment matches sources {}.".format(json_filename))

        # Clean up
        del diagnostic_obj

    # This routine does not return any values


# ============================================================================================================
if __name__ == "__main__":
    # Testing
    import pickle

    pfile = sys.argv[1]
    filehandler = open(pfile, 'rb')
    total_obj_list = pickle.load(filehandler)

    log_level = logutil.logging.DEBUG

    test_compare_num_sources = False
    test_find_gaia_sources = True
    test_compare_ra_dec_crossmatches = False
    test_characterize_gaia_distribution = True
    test_compare_photometry = False

    # Test compare_num_sources
    if test_compare_num_sources:
        total_catalog_list = []
        total_drizzle_list = []
        for total_obj in total_obj_list:
            total_drizzle_list.append(total_obj.drizzle_filename)
            total_catalog_list.append(total_obj.point_cat_filename)
            total_catalog_list.append(total_obj.segment_cat_filename)
        compare_num_sources(total_catalog_list, total_drizzle_list, log_level=log_level)

    # test find_gaia_sources
    if test_find_gaia_sources:
        for total_obj in total_obj_list:
            find_gaia_sources(total_obj, log_level=log_level)
            for filter_obj in total_obj.fdp_list:
                find_gaia_sources(filter_obj, log_level=log_level)
                for exp_obj in filter_obj.edp_list:
                    find_gaia_sources(exp_obj, log_level=log_level)

    # test compare_ra_dec_crossmatches
    if test_compare_ra_dec_crossmatches:
        for total_obj in total_obj_list:
            for filter_obj in total_obj.fdp_list:
                compare_ra_dec_crossmatches(filter_obj, log_level=log_level)

    # test characterize_gaia_distribution
    if test_characterize_gaia_distribution:
        for total_obj in total_obj_list:
            for filter_obj in total_obj.fdp_list:
                characterize_gaia_distribution(filter_obj, log_level=log_level)

    # test compare_photometry
    if test_compare_photometry:
        tot_len = len(total_obj_list)
        filter_drizzle_list = []
        temp_list = []
        for tot in total_obj_list:
            temp_list = [x.drizzle_filename for x in tot.fdp_list]
            filter_drizzle_list.extend(temp_list)
        compare_photometry(filter_drizzle_list, log_level=log_level)