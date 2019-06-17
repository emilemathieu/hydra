import argparse
import itertools
import logging
import logging.config
import os
import sys
from time import strftime, localtime

import pkg_resources
from omegaconf import OmegaConf

from .task import Task
from .fairtask_launcher import FAIRTaskLauncher
from . import utils
from collections import OrderedDict

# add cwd to path to allow running directly from the repo top level directory
sys.path.append(os.getcwd())

log = None


def configure_log(cfg_dir, cfg, verbose=None):
    # configure target directory for all logs files (binary, text. models etc)
    log_dir_suffix = cfg.log_dir_suffix or strftime("%Y-%m-%d_%H-%M-%S", localtime())
    log_dir = os.path.join(cfg.log_dir or "logs", log_dir_suffix)
    cfg.full_log_dir = log_dir
    os.makedirs(cfg.full_log_dir, exist_ok=True)

    logging_config = cfg.logging.config
    if not os.path.isabs(logging_config):
        logging_config = os.path.join(cfg_dir, logging_config)

    logcfg = OmegaConf.load(logging_config)
    log_name = logcfg.handlers.file.filename
    if not os.path.isabs(log_name):
        logcfg.handlers.file.filename = os.path.join(cfg.full_log_dir, log_name)
    logging.config.dictConfig(logcfg.to_dict())

    global log
    log = logging.getLogger(__name__)

    if verbose:
        if verbose == 'root':
            logging.getLogger().setLevel(logging.DEBUG)
        for logger in verbose.split(','):
            logging.getLogger(logger).setLevel(logging.DEBUG)


def get_args():
    parser = argparse.ArgumentParser(description='Hydra experimentation framework')
    version = pkg_resources.require("hydra")[0].version
    parser.add_argument('--version', action='version', version="hydra {}".format(version))

    def add_default_switches(prs):
        prs.add_argument(
            help="Task directory or name",
            type=str,
            dest="task"
        )
        prs.add_argument('overrides', nargs='*', help="Any key=value arguments to override config values "
                                                      "(use dots for.nested=overrides)")

    subparsers = parser.add_subparsers(help="sub-command help", dest="command")
    parser.add_argument('--verbose', '-v',
                        help='Activate debug logging, otherwise takes a '
                             'comma separated list of loggers ("root" for root logger)',
                        nargs='?',
                        default='')

    cfg_parser = subparsers.add_parser("cfg", help="Show generated cfg")
    add_default_switches(cfg_parser)

    cfg_parser.add_argument('--debug', '-d', action="store_true", default=False,
                            help="Show how the config was generated")

    run_parser = subparsers.add_parser("run", help="Run a task")
    add_default_switches(run_parser)

    sweep_parser = subparsers.add_parser("sweep", help="Run a parameter sweep")
    add_default_switches(sweep_parser)

    return parser.parse_args()


def find_task(task_class):
    return utils.get_class(task_class)()


def find_cfg_dir(task_class):
    path = os.getcwd()
    paths = [path]
    for p in task_class.split('.'):
        path = os.path.realpath(os.path.join(path, p))
        paths.append(path)

    for p in reversed(paths):
        path = os.path.join(p, 'conf')
        if os.path.exists(p) and os.path.isdir(path):
            return path


# def to_ordered_dict(cfg, key_name):
#     lst = cfg[key_name]
#     if lst is None:
#         return OrderedDict()
#     if not lst.is_sequence():
#         raise ValueError("{} must be a list because loading is order sensitive".format(key_name))
#     ret = []
#     for d in lst:
#         ret.extend([(k, v) for k, v in d.items()])
#     return OrderedDict(ret)

def validate_hydra_cfg(hydra_cfg):
    order = hydra_cfg.load_order or []
    for key in (hydra_cfg.configs or {}):
        if key not in order:
            raise RuntimeError("'{}' load order is not specified in load_order".format(key))


def create_task_cfg(cfg_dir, args):
    loaded_configs = []
    all_config_checked = []

    def load_config(filename):
        loaded_cfg = None
        if os.path.exists(filename):
            loaded_cfg = OmegaConf.load(filename)
            loaded_configs.append(filename)
            all_config_checked.append((filename, True))
        else:
            all_config_checked.append((filename, False))
        return loaded_cfg

    def merge_config(cfg_, family_, name_, required):
        family_dir = os.path.join(cfg_dir, family_)
        path = os.path.join(family_dir, name_) + '.yaml'
        new_cfg = load_config(path)
        if new_cfg is None:
            if required:
                options = [f[0:-len('.yaml')] for f in os.listdir(family_dir) if
                           os.path.isfile(os.path.join(family_dir, f)) and f.endswith(".yaml")]
                raise IOError("Could not load {}, available options : {}".format(path, ",".join(options)))
            else:
                return cfg_
        else:
            return OmegaConf.merge(cfg_, new_cfg)

    task_name = args.task.split('.')[-1]
    hydra_cfg_path = os.path.join(cfg_dir, "hydra.yaml")
    hydra_cfg = OmegaConf.load(hydra_cfg_path)

    # split overrides into defaults (which cause additional configs to be loaded)
    # and overrides which triggers overriding of specific nodes in the config tree
    overrides = []
    for override in args.overrides:
        key, value = override.split('=')
        path = os.path.join(cfg_dir, key)
        if os.path.exists(path):
            hydra_cfg.configs[key] = value
        else:
            overrides.append(override)

    validate_hydra_cfg(hydra_cfg)

    main_conf = os.path.join(cfg_dir, "{}.yaml".format(task_name))
    cfg = load_config(main_conf)
    if cfg is None:
        raise IOError("Could not load {}".format(main_conf))
    for family in hydra_cfg.load_order:
        name = hydra_cfg.configs[family]
        is_optional = family in (hydra_cfg.optional or [])
        cfg = merge_config(cfg, family, name, required=not is_optional)

    cfg = OmegaConf.merge(cfg, OmegaConf.from_cli(overrides))
    return dict(cfg=cfg, loaded=loaded_configs, checked=all_config_checked)


def run(args):
    cfg_dir = find_cfg_dir(args.task)
    task_cfg = create_task_cfg(cfg_dir, args)
    cfg = task_cfg['cfg']
    configure_log(cfg_dir, cfg, args.verbose)
    task = find_task(args.task)
    assert isinstance(task, Task)
    task.setup(cfg)
    task.run(cfg)


def cfg(args):
    cfg_dir = find_cfg_dir(args.task)
    task_cfg = create_task_cfg(cfg_dir, args)
    cfg = task_cfg['cfg']
    configure_log(cfg_dir, cfg, args.verbose)
    if args.debug:
        for file, loaded in task_cfg['checked']:
            if loaded:
                print("Loaded: {}".format(file))
            else:
                print("Not found: {}".format(file))
    print(cfg.pretty())


def get_sweep(overrides):
    lists = []
    for s in overrides:
        key, value = s.split('=')
        lists.append(["{}={}".format(key, value) for value in value.split(',')])

    return list(itertools.product(*lists))


def sweep(args):
    cfg_dir = find_cfg_dir(args.task)
    # task_cfg = create_task_cfg(cfg_dir, args)
    # cfg = task_cfg['cfg']
    # configure_log(cfg_dir, cfg, args.verbose)

    sweep_configs = get_sweep(args.overrides)
    launcher = FAIRTaskLauncher(cfg_dir)
    launcher.launch(sweep_configs)


def main():
    args = get_args()
    if args.command == 'run':
        run(args)
    elif args.command == 'cfg':
        cfg(args)
    elif args.command == 'sweep':
        sweep(args)


if __name__ == '__main__':
    main()
