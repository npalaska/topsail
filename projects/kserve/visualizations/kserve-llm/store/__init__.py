import pathlib
import logging
import types
import pickle
import fnmatch
import os
import json

import matrix_benchmarking.store as store
import matrix_benchmarking.store.simple as store_simple

from . import parsers
from . import lts_parser

CACHE_FILENAME = "cache.pickle"

IMPORTANT_FILES = parsers.IMPORTANT_FILES

from ..models import lts as models_lts

def is_mandatory_file(filename):
    return filename.name in ("settings", "exit_code", "config.yaml") or filename.name.startswith("settings.")


def is_important_file(filename):
    if str(filename) in IMPORTANT_FILES:
        return True

    for important_file in IMPORTANT_FILES:
        if "*" not in important_file: continue

        if fnmatch.filter([str(filename)], important_file):
            return True

    return False


def is_cache_file(filename):
    return filename.name == CACHE_FILENAME


def resolve_artifact_dirnames(dirname, artifact_dirnames):
    artifact_paths = types.SimpleNamespace()
    for artifact_dirname, unresolved_dirname in artifact_dirnames.__dict__.items():
        direct_resolution = dirname / unresolved_dirname
        resolutions = list(dirname.glob(unresolved_dirname))
        resolved_dir = None

        if direct_resolution.exists():
            # all good
            resolved_dir = direct_resolution.relative_to(dirname)
        elif not resolutions:
            logging.warning(f"Cannot resolve {artifact_dirname} glob '{dirname / unresolved_dirname}'")
        else:
            if len(resolutions) > 1:
                logging.info(f"Found multiple resolutions for {artifact_dirname} glob '{unresolved_dirname}' in '{dirname}': {resolutions}.")

                resolved_dir = [r.relative_to(dirname) for r in sorted(resolutions)]
            else:
                resolved_dir = resolutions[0].relative_to(dirname)

        artifact_paths.__dict__[artifact_dirname] = resolved_dir

    return artifact_paths


def register_important_file(base_dirname, filename):
    if not is_important_file(filename):
        logging.warning(f"File '{filename}' not part of the important file list :/")
        if pathlib.Path(filename).is_absolute():
            logging.warning(f"File '{filename}' is an absolute path. Should be relative to {base_dirname}.")
    return base_dirname / filename

parsers.register_important_file = register_important_file


def _rewrite_settings(settings_dict):
    return settings_dict


def load_cache(dirname):
    with open(dirname / CACHE_FILENAME, "rb") as f:
        return pickle.load(f)


def _parse_directory(fn_add_to_matrix, dirname, import_settings):
    parsers.artifact_paths = resolve_artifact_dirnames(dirname, parsers.artifact_dirnames)

    ignore_cache = os.environ.get("MATBENCH_STORE_IGNORE_CACHE", False) in ("yes", "y", "true", "True")
    if not ignore_cache:
        try:
            results = load_cache(dirname)
        except FileNotFoundError:
            results = None # Cache file doesn't exit, ignore and parse the artifacts
    else:
        logging.info("MATBENCH_STORE_IGNORE_CACHE is set, not processing the cache file.")
        results = None


    if results:
        parsers._parse_always(results, dirname, import_settings)

        fn_add_to_matrix(results)

        return

    results = types.SimpleNamespace()

    parsers._parse_always(results, dirname, import_settings)
    parsers._parse_once(results, dirname)

    lts_results = lts_parser.generate_lts_results(results)
    results.lts = lts_parser.generate_lts_payload(results, lts_results, import_settings, must_validate=False)

    fn_add_to_matrix(results)

    with open(dirname / "test_start_end.json", "w") as f:
        json.dump(dict(
            start=results.test_start_end.start.isoformat(),
            end=results.test_start_end.end.isoformat(),
            settings=import_settings,
        ), f, indent=4)
        print("", file=f)

    with open(dirname / CACHE_FILENAME, "wb") as f:
        get_config = results.test_config.get
        results.test_config.get = None

        pickle.dump(results, f)

        results.test_config.get = get_config

    logging.info("parsing done :)")

# delegate the parsing to the simple_store
store.register_custom_rewrite_settings(_rewrite_settings)
store_simple.register_custom_parse_results(_parse_directory)

store.register_lts_schema(models_lts.Payload)
from . import lts
build_lts_payloads = lts.build_lts_payloads
