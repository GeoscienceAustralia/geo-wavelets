"""
A pipeline for learning and validating models.
"""

import copy
import importlib.machinery
import json
import logging
import pickle
import sys
from collections import OrderedDict
from glob import glob
from os import mkdir, path

import numpy as np

from uncoverml import datatypes
from uncoverml import geoio
from uncoverml import mpiops
from uncoverml import pipeline
from uncoverml.validation import lower_is_better

# Logging
log = logging.getLogger(__name__)


def make_proc_dir(dirname):
    if not path.exists(dirname):
        mkdir(dirname)
        log.info("Made processed dir")


def get_targets(shapefile, fieldname, folds, seed):
    shape_infile = path.abspath(shapefile)
    lonlat, vals = geoio.load_shapefile(shape_infile, fieldname)
    targets = datatypes.CrossValTargets(lonlat, vals, folds, seed, sort=True)
    return targets


def extract(targets, config):
    # Extract feats for training
    tifs = glob(path.join(config.data_dir, "*.tif"))
    if len(tifs) == 0:
        log.fatal("No geotiffs found in {}!".format(config.data_dir))
        sys.exit(-1)

    extracted_chunks = {}
    settings = {}
    for tif in tifs:
        name = path.basename(tif)
        log.info("Processing {}.".format(name))
        s = datatypes.ExtractSettings(onehot=config.onehot,
                                      x_sets=None,
                                      patchsize=config.patchsize)
        image_source = geoio.RasterioImageSource(tif)
        # x may be none, but everyone gets the same settings object
        x, s = pipeline.extract_features(image_source, targets, s)
        extracted_chunks[name] = x
        settings[name] = s
    result = OrderedDict(sorted(extracted_chunks.items(), key=lambda t: t[0]))
    return result, settings


def run_pipeline(config):

    # Make the targets
    shapefile = path.join(config.data_dir, config.target_file)
    targets = mpiops.run_once(get_targets,
                              shapefile=shapefile,
                              fieldname=config.target_var,
                              folds=config.folds,
                              seed=config.crossval_seed)
    if config.export_targets:
        outfile_targets = path.join(config.output_dir,
                                    config.name + "_targets.hdf5")
        mpiops.run_once(geoio.write_targets, targets, outfile_targets)

    # keys for these two are the filenames
    extracted_chunks, image_settings = extract(targets, config)

    compose_settings = datatypes.ComposeSettings(
        impute=config.impute,
        transform=config.transform,
        featurefraction=config.pca_frac,
        impute_mean=None,
        mean=None,
        sd=None,
        eigvals=None,
        eigvecs=None)

    for algorithm in sorted(config.algdict.keys()):
        args = config.algdict[algorithm]
        if config.rank_features:
            measures, features, scores = rank_features(extracted_chunks,
                                                       targets, algorithm,
                                                       compose_settings,
                                                       config)
            mpiops.run_once(export_feature_ranks, measures,
                            features, scores, algorithm, config)

    # all nodes need to agree on the order of iteration
    X = gather_data(extracted_chunks, compose_settings)

    for algorithm in sorted(config.algdict.keys()):
        args = config.algdict[algorithm]

        if config.cross_validate:
            crossval_results = pipeline.cross_validate(X, targets, algorithm,
                                                       args)
            mpiops.run_once(export_scores, crossval_results, algorithm, config)

        model = pipeline.learn_model(X, targets, algorithm, args)
        mpiops.run_once(export_model, model, image_settings,
                        compose_settings, algorithm, config)


def rank_features(extracted_chunks, targets, algorithm, compose_settings,
                  config):

    # Determine the importance of each feature
    feature_scores = {}
    for name in extracted_chunks:
        dict_missing = dict(extracted_chunks)
        del dict_missing[name]

        fname = name.rstrip(".tif")
        log.info("Computing {} feature importance of {}"
                 .format(algorithm, fname))

        compose_missing = copy.deepcopy(compose_settings)
        X = gather_data(dict_missing, compose_missing)
        results = pipeline.cross_validate(X, targets, algorithm,
                                          config.algdict[algorithm])
        feature_scores[fname] = results

    # Get the different types of score from one of the outputs
    # TODO make this not suck
    measures = list(next(feature_scores.values().__iter__()).scores.keys())
    features = sorted(feature_scores.keys())
    scores = np.empty((len(measures), len(features)))
    for m, measure in enumerate(measures):
        for f, feature in enumerate(features):
            scores[m, f] = feature_scores[feature].scores[measure]
    return measures, features, scores


def gather_data(extracted_chunks, compose_settings):
    has_data = not (True in [k is None for k in extracted_chunks.values()])
    if has_data:
        x = np.ma.concatenate(extracted_chunks.values(), axis=1)
    else:
        x = None
    x_out, compose_settings = pipeline.compose_features(x, compose_settings)

    X_list = mpiops.comm.allgather(x_out)
    X = np.ma.vstack([k for k in X_list if k is not None])
    return X


def export_feature_ranks(measures, features, scores, algorithm, config):
    outfile_ranks = path.join(config.output_dir,
                              config.name + "_" + algorithm +
                              "_featureranks.json")

    score_listing = dict(scores={}, ranks={})
    for measure, measure_scores in zip(measures, scores):

        # Sort the scores
        scores = sorted(zip(features, measure_scores),
                        key=lambda s: s[1])

        # if measure in lower_is_better:
        #     scores.reverse()


        sorted_features, sorted_scores = zip(*scores)

        # Store the results
        score_listing['scores'][measure] = sorted_scores
        score_listing['ranks'][measure] = sorted_features

    # Write the results out to a file
    with open(outfile_ranks, 'w') as output_file:
        json.dump(score_listing, output_file, sort_keys=True, indent=4)


def export_model(model, image_settings, compose_settings, algorithm, config):
    outfile_state = path.join(config.output_dir,
                              config.name + "_" + algorithm + ".state")
    state_dict = {"model": model,
                  "image_settings": image_settings,
                  "compose_settings": compose_settings}

    with open(outfile_state, 'wb') as f:
        pickle.dump(state_dict, f)


def export_scores(crossval_output, algorithm, config):

    outfile_scores = path.join(config.output_dir,
                               config.name + "_" + algorithm +
                               "_scores.json")
    geoio.export_scores(crossval_output.scores,
                        crossval_output.y_true,
                        crossval_output.y_pred,
                        outfile_scores)


def main():
    if len(sys.argv) != 2:
        sys.exit(-1)
    logging.basicConfig(level=logging.INFO)
    config_filename = sys.argv[1]
    name = path.basename(config_filename).rstrip(".pipeline")
    config = importlib.machinery.SourceFileLoader(
        'config', config_filename).load_module()
    if not hasattr(config, 'name'):
        config.name = name
    config.output_dir = path.abspath(config.output_dir)
    run_pipeline(config)

if __name__ == "__main__":
    main()
