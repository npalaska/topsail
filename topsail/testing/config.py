import logging
logging.getLogger().setLevel(logging.INFO)
import os
import pathlib
import yaml
import shutil
import subprocess
import threading

import jsonpath_ng

from . import env
from . import run

VARIABLE_OVERRIDES_FILENAME = "variable_overrides"
PR_ARG_KEY = "PR_POSITIONAL_ARG_"

ci_artifacts = None # will be set in init()

class TempValue(object):
    def __init__(self, config, key, value):
        self.config = config
        self.key = key
        self.value = value
        self.prev_value = None

    def __enter__(self):
        self.prev_value = self.config.get_config(self.key)
        self.config.set_config(self.key, self.value)

        return True

    def __exit__(self, ex_type, ex_value, exc_traceback):
        self.config.set_config(self.key, self.prev_value)

        return False # If we returned True here, any exception would be suppressed!


class Config:
    def __init__(self, config_path):
        self.config_path = config_path

        if not self.config_path.exists():
            msg = f"Configuration file '{self.config_path}' does not exist :/"
            logging.error(msg)
            raise ValueError(msg)

        with open(self.config_path) as config_f:
            self.config = yaml.safe_load(config_f)

    def apply_local_config_overrides(self):
        TOPSAIL_PR_ARGS_KEY = "TOPSAIL_PR_ARGS"
        pr_args = os.environ.get(TOPSAIL_PR_ARGS_KEY)
        if not pr_args:
            logging.info(f"{TOPSAIL_PR_ARGS_KEY} env var not set, no local config to override.")
            return

        if ocp_ci := os.environ.get("OPENSHIFT_CI"):
            raise RuntimeError(f"Found {TOPSAIL_PR_ARGS_KEY}={pr_args} and OPENSHIFT_CI={ocp_ci} defined at the same time, this isn't expected ...")

        for idx, arg in enumerate(pr_args.split()):
            key = f"{PR_ARG_KEY}{idx + 1}"
            logging.info(f"{key} --> {arg}")
            self.config[key] = arg


    def apply_config_overrides(self):
        variable_overrides_path = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME

        if not variable_overrides_path.exists():
            logging.info(f"apply_config_overrides: {variable_overrides_path} does not exist, nothing to override.")
            return

        with open(variable_overrides_path) as f:
            for line in f.readlines():
                if not line.strip():
                    continue

                key, found, _value = line.strip().partition("=")
                if not found:
                    logging.warning(f"apply_config_overrides: Invalid line: '{line.strip()}', ignoring it.")
                    continue

                value = yaml.safe_load(_value) # convert the string as YAML would do

                MAGIC_DEFAULT_VALUE = object()
                current_value = self.get_config(key, MAGIC_DEFAULT_VALUE)
                if current_value == MAGIC_DEFAULT_VALUE:
                    if "." in key:
                        raise ValueError(f"Config key '{key}' does not exist, and cannot create it at the moment :/")
                    self.config[key] = None

                self.set_config(key, value, dump_command_args=False)
                actual_value = self.get_config(key) # ensure that key has been set, raises an exception otherwise
                logging.info(f"config override: {key} --> {actual_value}")

    def apply_preset(self, name):
        try:
            values = self.get_config(f'ci_presets["{name}"]')
        except IndexError:
            logging.error(f"Preset '{name}' does not exists :/")
            raise

        logging.info(f"Appling preset '{name}' ==> {values}")
        if not values:
            raise ValueError("Preset '{name}' does not exists")

        presets = self.get_config("ci_presets.names") or []
        if not name in presets:
            self.set_config("ci_presets.names", presets + [name], dump_command_args=False)

        for key, value in values.items():
            if key == "extends":
                for extend_name in value:
                    self.apply_preset(extend_name)
                continue

            msg = f"preset[{name}] {key} --> {value}"
            logging.info(msg)
            with open(env.ARTIFACT_DIR / "presets_applied", "a") as f:
                print(msg, file=f)

            self.set_config(key, value, dump_command_args=False)

        self.dump_command_args()

    def get_config(self, jsonpath, default_value=..., warn=True, print=True):
        try:
            value = jsonpath_ng.parse(jsonpath).find(self.config)[0].value
        except IndexError as ex:
            if default_value != ...:
                if warn:
                    logging.warning(f"get_config: {jsonpath} --> missing. Returning the default value: {default_value}")
                return default_value

            logging.error(f"get_config: {jsonpath} --> {ex}")
            raise KeyError(f"Key '{jsonpath}' not found in {self.config_path}")

        if print:
            logging.info(f"get_config: {jsonpath} --> {value}")

        return value


    def set_config(self, jsonpath, value, dump_command_args=True):
        if threading.current_thread().name != "MainThread":
            msg = f"set_config({jsonpath}, {value}) cannot be called from a thread, to avoid race conditions."
            if os.environ.get("OPENSHIFT_CI") or os.environ.get("PERFLAB_CI"):
                logging.error(msg)
                with open(env.ARTIFACT_DIR / "SET_CONFIG_CALLED_FROM_THREAD", "a") as f:
                    print(msg, file=f)
            else:
                raise RuntimeError(msg)

        try:
            self.get_config(jsonpath, value) # will raise an exception if the jsonpath does not exist
            jsonpath_ng.parse(jsonpath).update(self.config, value)
        except Exception as ex:
            logging.error(f"set_config: {jsonpath}={value} --> {ex}")
            raise

        logging.info(f"set_config: {jsonpath} --> {value}")

        with open(self.config_path, "w") as f:
            yaml.dump(self.config, f, indent=4, default_flow_style=False, sort_keys=False)

        if dump_command_args:
            self.dump_command_args()

        if (shared_dir := os.environ.get("SHARED_DIR")) and (shared_dir_path := pathlib.Path(shared_dir)) and shared_dir_path.exists():

            with open(shared_dir_path / "config.yaml", "w") as f:
                yaml.dump(self.config, f, indent=4)

    def dump_command_args(self):
        try:
            command_template = get_command_arg("dump", "config", None)
        except Exception as e:
            import traceback
            with open(env.ARTIFACT_DIR / "command_args.yml", "w") as f:
                traceback.print_exc(file=f)
            logging.warning("Could not dump the command_args template.")
            return

        with open(env.ARTIFACT_DIR / "command_args.yml", "w") as f:
            print(command_template, file=f)

    def apply_preset_from_pr_args(self):
        for config_key in self.get_config("$", print=False).keys():
            if not config_key.startswith(PR_ARG_KEY): continue
            if config_key == f"{PR_ARG_KEY}0": continue

            for preset in self.get_config(config_key).strip().split(" "):
                self.apply_preset(preset)

    def detect_apply_light_profile(self, profile, name_suffix="light"):
        job_name_safe = os.environ.get("JOB_NAME_SAFE", "")
        if not job_name_safe:
            logging.info(f"detect_apply_light_profile: JOB_NAME_SAFE not set, assuming not running in a CI environment.")
            return

        if job_name_safe != name_suffix and not job_name_safe.endswith(f"-{name_suffix}"):
            return

        logging.info(f"Running a '{name_suffix}' test ({job_name_safe}), applying the '{profile}' profile")

        self.apply_preset(profile)


    def detect_apply_metal_profile(self, profile):
        platform_type_cmd = run.run("oc get infrastructure/cluster -ojsonpath={.status.platformStatus.type}", capture_stdout=True, capture_stderr=True, check=False)
        if platform_type_cmd.returncode != 0:
            logging.warning(f"Failed to get the platform type: {platform_type_cmd.stderr.strip()}")
            logging.warning("Ignoring the metal profile check.")
            return

        platform_type = platform_type_cmd.stdout
        logging.info(f"detect_apply_metal_profile: infrastructure/cluster.status.platformStatus.type = {platform_type}")
        if platform_type not in ("BareMetal", "None"):
            logging.info("detect_apply_metal_profile: Assuming not running in a bare-metal environment.")
            return
        logging.info(f"detect_apply_metal_profile: Assuming running in a bare-metal environment. Applying the '{profile}' profile.")

        self.apply_preset(profile)


def _set_config_environ(base_dir):
    config_path = env.ARTIFACT_DIR / "config.yaml"

    os.environ["CI_ARTIFACTS_FROM_CONFIG_FILE"] = str(config_path)
    os.environ["CI_ARTIFACTS_FROM_COMMAND_ARGS_FILE"] = str(base_dir / "command_args.yml.j2")

    if base_dir != env.ARTIFACT_DIR:
        # make sure we're using a clean copy of the configuration file
        config_path.unlink(missing_ok=True)

    if shared_dir := os.environ.get("SHARED_DIR"):
        shared_dir_config_path = pathlib.Path(shared_dir) / "config.yaml"
        if shared_dir_config_path.exists():
            logging.info(f"Reloading the config file from {shared_dir_config_path} ...")
            shutil.copyfile(shared_dir_config_path, config_path)

    if not config_path.exists():
        shutil.copyfile(base_dir / "config.yaml", config_path)


    return config_path


def get_command_arg(group, command, arg, prefix=None, suffix=None):
    try:
        logging.info(f"get_command_arg: {group} {command} {arg}")
        proc = run.run_toolbox_from_config(group, command, show_args=arg,
                                           prefix=prefix, suffix=suffix,
                                           check=True, run_kwargs=dict(capture_stdout=True, capture_stderr=True))
    except subprocess.CalledProcessError as e:
        logging.error(e.stderr.strip().encode("ascii", "ignore"))
        raise

    return proc.stdout.strip()


def set_jsonpath(config, jsonpath, value):
    get_jsonpath(config, jsonpath) # will raise an exception if the jsonpath does not exist
    jsonpath_ng.parse(jsonpath).update(config, value)

def get_jsonpath(config, jsonpath):
    return jsonpath_ng.parse(jsonpath).find(config)[0].value


def init(base_dir):
    global ci_artifacts

    if ci_artifacts:
        logging.info("config.init: already configured.")
        return

    config_path = _set_config_environ(base_dir)
    ci_artifacts = Config(config_path)

    logging.info("config.init: apply the ci-artifacts config overrides")
    ci_artifacts.apply_config_overrides()
    ci_artifacts.apply_local_config_overrides()
