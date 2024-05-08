# -*- coding: utf-8 -*-
"""Pipeline tools for OpenPype Gaffer integration."""
import os
import sys
import json

import Gaffer  # noqa

from ayon_core.host import HostBase, IWorkfileHost, ILoadHost, IPublishHost
from ayon_gaffer.api.nodes import RenderLayerNode

import pyblish.api

from ayon_core.pipeline import (
    register_creator_plugin_path,
    register_loader_plugin_path,
    AVALON_CONTAINER_ID,
    AYON_CONTAINER_ID,
    get_current_folder_path,
    get_current_task_name,
)
from ayon_gaffer import GAFFER_HOST_DIR
import ayon_gaffer.api.nodes
import ayon_gaffer.api.lib
from ayon_core.lib import Logger

log = Logger.get_logger("ayon_gaffer.api.pipeline")

PLUGINS_DIR = os.path.join(GAFFER_HOST_DIR, "plugins")
PUBLISH_PATH = os.path.join(PLUGINS_DIR, "publish")
LOAD_PATH = os.path.join(PLUGINS_DIR, "load")
CREATE_PATH = os.path.join(PLUGINS_DIR, "create")
INVENTORY_PATH = os.path.join(PLUGINS_DIR, "inventory")

self = sys.modules[__name__]
self.root = None

# A prefix used for storing JSON blobs in string plugs
JSON_PREFIX = "JSON:::"


def set_root(root: Gaffer.ScriptNode):
    self.root = root


def get_root() -> Gaffer.ScriptNode:
    return self.root


class GafferHost(HostBase, IWorkfileHost, ILoadHost, IPublishHost):
    name = "gaffer"

    _context_plug = "ayon_context"

    def __init__(self, application):
        super(GafferHost, self).__init__()
        self.application = application

    def install(self):
        pyblish.api.register_host("gaffer")

        pyblish.api.register_plugin_path(PUBLISH_PATH)
        register_loader_plugin_path(LOAD_PATH)
        register_creator_plugin_path(CREATE_PATH)
        log.info("Registering paths")
        log.info(PUBLISH_PATH)
        log.info(LOAD_PATH)
        log.info(CREATE_PATH)

        self._register_callbacks()

    def has_unsaved_changes(self):
        script = get_root()
        return script["unsavedChanges"].getValue()

    def get_workfile_extensions(self):
        return [".gfr"]

    def save_workfile(self, dst_path=None):
        if not dst_path:
            dst_path = self.get_current_workfile()

        dst_path = dst_path.replace("\\", "/")

        script = get_root()
        script.serialiseToFile(dst_path)
        script["fileName"].setValue(dst_path)
        script["unsavedChanges"].setValue(False)

        application = script.ancestor(Gaffer.ApplicationRoot)
        if application:
            import GafferUI.FileMenu
            GafferUI.FileMenu.addRecentFile(application, dst_path)

        self.update_project_root_directory(script)

        return dst_path

    def open_workfile(self, filepath):
        if not os.path.exists(filepath):
            raise RuntimeError("File does not exist: {}".format(filepath))

        script = get_root()
        if script:
            script["fileName"].setValue(filepath)
            script.load()
        self._on_scene_new(script.ancestor(Gaffer.ScriptContainer), script)
        return filepath

    def get_current_workfile(self):
        script = get_root()
        return script["fileName"].getValue()

    def get_containers(self):
        script = get_root()

        required = [
            "schema", "id", "name", "namespace", "representation", "loader"
        ]

        for node in script.children(Gaffer.Node):
            if "user" not in node:
                # No user attributes
                continue

            user = node["user"]
            if any(key not in user for key in required):
                continue

            if user["id"].getValue() not in {AYON_CONTAINER_ID, AVALON_CONTAINER_ID}:
                continue
            container = {
                key: user[key].getValue() for key in required
            }
            container["objectName"] = node.fullName()
            container["_node"] = node

            yield container

    def update_context_data(self, data, changes):
        """Store context data as single JSON blob in script's user data"""
        script = get_root()
        data_str = json.dumps(data)

        # Always override the full plug - even if it already exists
        script["user"][self._context_plug] = Gaffer.StringPlug(
            defaultValue=data_str,
            flags=Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic
        )

    def get_context_data(self):
        script = get_root()
        if "user" in script and self._context_plug in script["user"]:
            data_str = script["user"][self._context_plug].getValue()
            return json.loads(data_str)
        return {}

    def _register_callbacks(self):
        scripts_list = self.application.root()["scripts"]
        scripts_list.childAddedSignal().connect(self._on_scene_new,
                                                scoped=False)

    def update_project_root_directory(self, script_node):
        log.info("updating project root directory")
        script_node['variables']['projectRootDirectory']['value'].setValue(
            self.work_root(os.environ))  # noqa

    def update_root_context_variables(self, script_node):
        ctxt = self.get_current_context()

        ayon_gaffer.api.lib.update_root_context_variables(
            script_node,
            ctxt["project_name"],
            ctxt["folder_path"]
        )

    def _on_scene_new(self, script_container, script_node):
        # Update the projectRootDirectory variable for new workfile scripts
        self.update_project_root_directory(script_node)
        self.update_root_context_variables(script_node)
        ayon_gaffer.api.lib.create_multishot_context_vars(script_node)
        ayon_gaffer.api.lib.set_framerate(script_node)
        log.debug(f'Adding childAddedSignal to {script_node}')
        script_node.childAddedSignal().connect(
            self.connect_render_layer_signals,
            scoped=False
        )

        # since the childAddedSignal gets added after the initial scene is
        # loaded we need to manually trigger the connect render layer
        # signal for the renderlayer nodes in the scene
        for node in script_node.children(RenderLayerNode):
            self.connect_render_layer_signals(script_node, node)

        ayon_gaffer.api.nodes.check_boxnode_versions(script_node)

    def connect_render_layer_signals(self, script_node, new_node):
        if isinstance(new_node, RenderLayerNode):
            try:
                new_node.connect_signals()
                # new_node.update_outputs()
            except Exception as err:
                log.error(f"Could not connect signals for render layer"
                          f"{new_node}: {err}")


def imprint_container(node: Gaffer.Node,
                      name: str,
                      namespace: str,
                      context: dict,
                      loader: str = None):
    """Imprint a Loader with metadata

    Containerisation enables a tracking of version, author and origin
    for loaded assets.

    Arguments:
        node (Gaffer.Node): The node in Gaffer to imprint as container,
            usually a node loaded by a Loader.
        name (str): Name of resulting assembly
        namespace (str): Namespace under which to host container
        context (dict): Asset information
        loader (str, optional): Name of loader used to produce this container.

    Returns:
        None

    """
    data = {
        "schema": "openpype:container-2.0",
        "id": AYON_CONTAINER_ID,
        "name": str(name),
        "namespace": str(namespace),
        "loader": str(loader),
        "representation": str(context["representation"]["id"]),
    }
    imprint(node, data)


def imprint(node: Gaffer.Node,
            data: dict,
            section: str = "Ayon"):
    """Store and persist data on a node as `user` data.

    Args:
        node (Gaffer.Node): The node to store the data on.
            This can also be the workfile's root script node.
        data (dict): The key, values to store.
            Any `dict` values will be treated as JSON data and stored as
            string with `JSON:::` as a prefix to the value.
        section (str): Used to register the plug into a subsection in
            the user data allowing them to group data together.

    Returns:

    """

    FLAGS = Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic

    for key, value in data.items():
        # Dict to JSON
        if isinstance(value, dict):
            value = json.dumps(value)
            value = f"{JSON_PREFIX}{value}"

        if key in node["user"]:
            # Set existing attribute
            try:
                node["user"][key].setValue(value)
                continue
            except Exception:
                # If an exception occurs then we'll just replace the key
                # with a new plug (likely types have changed)
                log.warning("Unable to set %s attribute %s to value %s (%s). "
                            "Likely there is a value type mismatch. "
                            "Plug will be replaced.",
                            node.getName(), key, value, type(value),
                            exc_info=sys.exc_info())
                pass

        if value is None:
            value = "<None>"

        # Generate new plug with value as default value
        if isinstance(value, str):
            plug = Gaffer.StringPlug(key, defaultValue=value, flags=FLAGS)
        elif isinstance(value, bool):
            plug = Gaffer.BoolPlug(key, defaultValue=value, flags=FLAGS)
        elif isinstance(value, float):
            plug = Gaffer.FloatPlug(key, defaultValue=value, flags=FLAGS)
        elif isinstance(value, int):
            plug = Gaffer.IntPlug(key, defaultValue=value, flags=FLAGS)
        else:
            raise TypeError(
                f"Unsupported value type: {type(value)} -> {value}"
            )

        if section:
            Gaffer.Metadata.registerValue(plug, "layout:section", section)

        node["user"][key] = plug


def get_context_label():
    return "{0}, {1}".format(
        get_current_folder_path(),
        get_current_task_name()
    )
