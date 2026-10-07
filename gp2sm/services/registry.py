"""Service discovery: built-in and third-party photo services, via the `gp2sm.services` entry-point group.

A plugin package declares, in its pyproject.toml:

    [project.entry-points."gp2sm.services"]
    myservice = "my_package:service"

where `service()` returns a ServiceInfo. A destination's factory must return a PhotoDestination (and pass
tests/contracts/destination.py); a source's factory must return a PhotoSource (tests/contracts/source.py).
"""

import importlib
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Callable

from gp2sm.services.base import PhotoDestination, PhotoSource

log = logging.getLogger("gp2sm.services")
GROUP = "gp2sm.services"
BUILTIN = ("gp2sm.smugmug:service", "gp2sm.takeout:service")


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    kind: str                 # "destination" | "source"
    factory: Callable         # keyword arguments -> PhotoDestination / PhotoSource
    description: str = ""


def _entry_points():
    return list(entry_points(group=GROUP))


def _load(target):
    module, attr = target.split(":")
    return getattr(importlib.import_module(module), attr)()


def available():
    """name -> ServiceInfo. Built-ins first; a third-party plugin can't shadow a built-in name."""
    infos = {}
    for target in BUILTIN:
        info = _load(target)
        infos[info.name] = info
    for ep in _entry_points():
        if ep.value in BUILTIN:
            continue  # built-ins also register as entry points (dogfooding); already loaded
        try:
            info = ep.load()()
        except Exception as e:  # a broken plugin must not break the tool
            log.warning("service plugin %s failed to load: %r", ep.name, e)
            continue
        if not isinstance(info, ServiceInfo) or info.kind not in ("destination", "source"):
            log.warning("service plugin %s returned an invalid ServiceInfo; ignored", ep.name)
            continue
        if info.name in infos:
            log.warning("service plugin %s tries to replace built-in %r; ignored", ep.name, info.name)
            continue
        infos[info.name] = info
    return infos


def _create(name, kind, protocol, kwargs):
    infos = available()
    if name not in infos:
        raise KeyError(f"unknown service {name!r}; available: {sorted(infos)}")
    info = infos[name]
    if info.kind != kind:
        raise TypeError(f"service {name!r} is a {info.kind}, not a {kind}")
    obj = info.factory(**kwargs)
    if not isinstance(obj, protocol):
        raise TypeError(f"service {name!r} does not implement {protocol.__name__}")
    return obj


def destination(name, **kwargs) -> PhotoDestination:
    return _create(name, "destination", PhotoDestination, kwargs)


def source(name, **kwargs) -> PhotoSource:
    return _create(name, "source", PhotoSource, kwargs)
