import sys
from queue import SimpleQueue
import re
from typing import Tuple, List, Optional

import Gaffer
import GafferScene
import imath

if sys.version_info >= (3, 9, 0):
    from collections.abc import Iterator
else:
    from typing import Iterator

from ayon_core.lib import Logger
from ayon_core.settings import get_project_settings
from ayon_core.pipeline import get_current_context

import ayon_core.lib
import ayon_api

log = Logger.get_logger('ayon_gaffer.api.lib')


def set_node_color(node: Gaffer.Node, color: Tuple[float, float, float]):
    """Set node color.

    Args:
        node (Gaffer.Node): Node to set the color for.
        color (tuple): 3 float values representing RGB between 0.0-1.0

    Returns:
        None

    """
    assert len(color) == 3, "Color must be three float values"
    Gaffer.Metadata.registerValue(node, "nodeGadget:color",
                                  imath.Color3f(*color))


def set_node_color_from_settings(node: Gaffer.Node, product_type: str):
    project_name = get_current_context()["project_name"]
    settings = get_project_settings(project_name)
    load_settings = settings.get("gaffer", {}).get("load", {})
    col_list = load_settings.get("product_colors", {}).get("color_list", [])

    for entry in col_list:
        print("!!!!", entry)
        if product_type.lower() == entry["name"].lower():
            set_node_color(node, entry["color"][:3])
            return
    log.warning(f"No color selected for product type: [{product_type}]")


def make_box(name: str,
             inputs: list = ["in"],
             outputs: list = ["out"],
             description: Optional[str] = None,
             hide_add_buttons: bool = True,
             connect_passthrough: bool = False) -> Gaffer.Box:
    """Create a Box node with BoxIn and BoxOut nodes

    Note:
        The box is not added as child anywhere - to have it visually
        appear make sure to call e.g. `parent.addChild(box)`

    Arguments:
        name (str): The name to give the box.
        inputs (list): List of input child plugs to add, an empty list creates
            no inputs
        outputs (list): List of child output plugs to add, empty list
            creates no output.
        description (Optional[str]): A description to register for the box.
        hide_add_buttons (bool): Whether the add buttons on the box
            node should be hidden or not. By default, this will hide them.
        connect_passthrough (bool): Should the first input be connected to the
            first outputs passthrough plug?

    Returns:
        Gaffer.Box: The created box

    """

    box = Gaffer.Box(name)

    if description:
        Gaffer.Metadata.registerValue(box, 'description', description)

    for inp in inputs:
        box_in = Gaffer.BoxIn(f"BoxIn_{inp}")
        box.addChild(box_in)
        box_in.setup(GafferScene.ScenePlug('out'))
        # set the newest plug name to the input name
        box.children()[-1].setName(inp)

    for outp in outputs:
        box_out = Gaffer.BoxOut(f"BoxOut_{outp}")

        box.addChild(box_out)
        box_out.setup(GafferScene.ScenePlug("in",))
        box.children()[-1].setName(outp)

    if hide_add_buttons:
        for key in [
            'noduleLayout:customGadget:addButtonTop:visible',
            'noduleLayout:customGadget:addButtonBottom:visible',
            'noduleLayout:customGadget:addButtonLeft:visible',
            'noduleLayout:customGadget:addButtonRight:visible',
        ]:
            Gaffer.Metadata.registerValue(box, key, False)

    if connect_passthrough and len(inputs) > 0 and len(outputs) > 0:
        first_input = box.children(Gaffer.BoxIn)[0]
        first_output = box.children(Gaffer.BoxOut)[0]
        first_output["passThrough"].setInput(first_input["out"])

    return box


def make_scene_load_box(
    scene_root,
    name,
    scenegraph_template,
    auxiliary_scengraph_transforms=[]
):
    '''
    Create a Box node to load a scene through. This facilitates placing the
    loaded geometry (or whatever) under certain groups in the scenegraph 
    (the `scenegraph_template` parameter). This also supports creating plugs
    for other groups you want underneath the root scenegraph template 
    group.

    Arguments:
        scene_root (Gaffer.ScriptNode): The current scriptnode.
        name (str): The name of the box node to be created.
        scengraph_template (str): Where the imported geo will be placed in the
            scenegraph, one template key is expanded, `{node}` which will be
            replaced with the value from the `name` parameter.
            Example: given the scenegraph_template value of '{node}/geo' and
            a name of 'IMPORT_NODE', that will result in the imported geo being
            placed at /IMPORT_NODE/geo/<imported geometry> in the scenegraph.
        auxiliary_scenegraph_transforms (list[str]): a list of other groups
            created under the top created transform, using the example above
            and a parameter value of ['mat', 'fur'] this would result in this
            scenegraph:
                /IMPORT_NODE/geo
                     |------/mat
                     `------/fur

    Returns:
        Gaffer.Box: the created box.

    '''
    box = make_box('scene_load_box', inputs=auxiliary_scengraph_transforms)
    box_name = get_next_valid_name(name, scene_root)
    box.setName(box_name)

    filename_plug = Gaffer.StringPlug(
        "fileName",
        defaultValue="",
        flags=Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic,
    )
    Gaffer.Metadata.registerValue(filename_plug, "nodule:type", "")
    box.addChild(filename_plug)

    # if the scenegraph template has subtransforms main/sub1/sub2 we want to
    # add plugs to disable those groupins, since we _might_ get stuff in that
    # already has thos groups.
    if "/" in scenegraph_template:
        scenegraph_template_parts = scenegraph_template.split("/")
        sc_root_name = scenegraph_template_parts[0]
        sub_groups = scenegraph_template_parts[1:]
    else:
        sc_root_name = scenegraph_template
        sub_groups = []
    print(f"scenegraph template root {sc_root_name}, sub groups: {sub_groups}")

    group_nodes = create_sub_groups(box, sub_groups)
    group_nodes.reverse()
    scene_reader = GafferScene.SceneReader()

    scene_reader["fileName"].setInput(filename_plug)
    scene_reader.setName("Read")
    box.addChild(scene_reader)

    if len(group_nodes) > 0:
        group_nodes[0]["in"][0].setInput(scene_reader["out"])
        current_group = group_nodes[0]
        for group in group_nodes[1:]:
            group["in"][0].setInput(current_group["out"])
            current_group = group
    else:
        print("*****")
        current_group = scene_reader

    merge_scenes = GafferScene.MergeScenes()
    box.addChild(merge_scenes)
    merge_scenes["in"][0].setInput(current_group["out"])

    main_group_name = sc_root_name.format(node=box_name)

    main_group = GafferScene.Group(main_group_name)
    main_group["name"].setValue(main_group_name)
    box.addChild(main_group)
    main_group["in"][0].setInput(merge_scenes["out"])
    box_outs = box.children(Gaffer.BoxOut)
    if len(box_outs) > 0:
        # connect the merge to the output
        box_outs[0]["in"].setInput(main_group["out"])

    # now handle aux transforms
    aux_groups = create_sub_groups(box, auxiliary_scengraph_transforms)
    idx = 1
    for grp, aux in zip(aux_groups, auxiliary_scengraph_transforms):
        grp["in"][0].setInput(box[f"BoxIn_{aux}"]["out"])
        merge_scenes["in"][idx].setInput(grp["out"])
        idx += 1

    return box


def create_sub_groups(parent, sub_groups):
    '''
    Given a parent box node and a list of group names this function adds
    GafferScene.Group nodes to the box, and returns them in the same order
    the sub_groups were in.

    This also adds bool plugs to the box where you can disable each group node
    The labels are concatenated with their preceding groups. Example:
        Given the sub_groups ['a','b','c']:
        'Enable /a' would disable or enable the ['a'] group node
        'Enable /a/b' would disable or enable the ['b'] group node
        'Enable /a/b/c' would disable or enable the ['c'] group node
    This function is mainly here to help make_scene_box function. But who knows
    maybe one day we'll get some use out of it.

    Arguments:
        parent (Gaffer.Box): The parent box, currently there is nothing that
            checks if this is actually a box node.
        sub_groups: list[str]: a list of groups to create, typically the result
            of "/a/b/c".split("/")

    Returns:
        list[GafferScene.Group]: The list of newly created gaffer group nodes.
    '''
    group_nodes = []
    for idx, grp in enumerate(sub_groups):
        print(f"** {grp} **")
        subs = "/".join(sub_groups[0:idx])
        if subs != "":
            subs = f"/{subs}"
        plug_label = f"Enable {subs}/{grp}"
        plug_name = plug_label.replace(" ", "_").replace("/", "_")
        plug = Gaffer.BoolPlug(
            plug_name,
            defaultValue=True,
            flags=Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic,
        )
        parent.addChild(plug)
        Gaffer.Metadata.registerValue(plug, "nodule:type", "")
        Gaffer.Metadata.registerValue(plug, "label", plug_label)

        group_node = GafferScene.Group(f"Group_{grp}")
        group_node["name"].setValue(grp)
        group_node["enabled"].setInput(plug)
        group_nodes.append(group_node)
        parent.addChild(group_node)
    return group_nodes


def get_next_valid_name(template, script_node):
    """
    Find the next number to replace a _##_ part of templates with.
    Given a template containing a single block of ## this function
    will traverse the nodegraph to find nodes with the same name (but a
    diferent number) and construct a unique name with the next highest number.

    Example:
        given the template 'node_###' and we already have 'node_001' and
            'node_002' in the scene, this function will return 'node_003'

    If no node is found with the name pattern 1 will be used.

    Arguments:
        template (str): The template string to format.
        script_node (Gaffer.ScriptNode): The script scriptNode
    """
    res = re.search(r'([a-zA-Z0-9_]*)(#+)([a-zA-Z0-9_]*)', template)
    if res is not None:
        print(res.group(1), res.group(2), res.group(3))
        head = res.group(1)
        padding = res.group(2)
        tail = res.group(3)
    else:
        head = template
        padding = ""
        tail = ""
        new_number = ""

    if padding:
        pad_len = len(padding)
        ex_names = []
        for child in script_node.children():
            if re.match(f"{head}.*{tail}", child.getName()):
                ex_names.append(child.getName())
        ex_names.sort(reverse=True)
        if len(ex_names) == 0:
            next_number = 1
        else:
            last_name = ex_names[0]

            res = re.search(r'(.*)_*(\d+)(.*)', last_name)
            if res is not None:
                next_number = int(res.group(2)) + 1
        new_number = str(next_number).zfill(pad_len)

    return f"{head}{new_number}{tail}"


def arrange(nodes: List[Gaffer.Node], parent: Optional[Gaffer.Node] = None):
    """Layout the nodes in the graph.

    Args:
        nodes (list): The nodes to rearrange into a nice layout.
        parent (list[Gaffer.Node]): Optional. The parent node to layout in.
            If not provided the parent of the first node is taken. The
            assumption is made that all nodes reside within the same parent.

    Returns:
        None

    """
    import GafferUI

    if not nodes:
        return

    if parent is None:
        # Assume passed in nodes all belong to single parent
        parent = nodes[0]

    graph = GafferUI.GraphGadget(parent)
    graph.getLayout().layoutNodes(graph, Gaffer.StandardSet(nodes))


def traverse_scene(scene_plug: GafferScene.ScenePlug,
                   root: str = "/") -> Iterator[str]:
    """Yields breadth first all children from given `root`.

    Note: This also yields the root itself.
    This traverses down without the need for a recursive function.

    Args:
        scene_plug (GafferScene.ScenePlug): Plug scene to traverse.
            Typically, the out plug of a node (`node["out"]`).
        root (string): The root path as starting point of the traversal.

    Yields:
        str: Child path

    """
    queue = SimpleQueue()
    queue.put_nowait(root)
    while not queue.empty():
        path = queue.get_nowait()
        yield path

        for child_name in scene_plug.childNames(path):
            child_path = f"{path.rstrip('/')}/{child_name}"
            queue.put_nowait(child_path)


def find_camera_paths(scene_plug: GafferScene.ScenePlug,
                      root: str = "/") -> List[str]:
    """Traverses the scene plug starting at `root` returning all cameras.

    Args:
        scene_plug (GafferScene.ScenePlug): Plug scene to traverse.
            Typically, the out plug of a node (`node["out"]`).
        root (string): The root path as starting point of the traversal.

    Returns:
        List[str]: List of found paths to cameras.

    """
    return find_paths_by_type(scene_plug, "Camera", root)


def find_paths_by_type(scene_plug: GafferScene.ScenePlug,
                       object_type_name: str,
                       root: str = "/") -> List[str]:
    """Return all paths in scene plug under `path` that match given type.

    Examples:
        >>> find_paths_by_type(plug, "MeshPrimitive")  # all meshes
        # ['/cube', '/nested/path/cube']
        >>> find_paths_by_type(plug, "NullObject")     # all nulls (groups)
        # ['/nested/path']
        >>> find_paths_by_type(plug, "Camera")         # all cameras
        # ['/camera', /nested/camera2']

    Args:
        scene_plug (GafferScene.ScenePlug): Plug scene to traverse.
            Typically, the out plug of a node (`node["out"]`).
        object_type_name (String): The name of the object type we want to find.
        root (string): Starting root path of traversal.

    Returns:
        List[str]: List of found paths matching the object type name.

    """
    result = []
    for path in traverse_scene(scene_plug, root):
        if scene_plug.object(path).typeName() == object_type_name:
            result.append(path)
    return result


def get_color_management_preferences(script_node):
    """Get default OCIO preferences"""
    return {
        "config": script_node['openColorIO']['config'].getValue(),
        "display": script_node['openColorIO']['displayTransform'].getValue(),
        "view": script_node['openColorIO']['workingSpace'].getValue()
    }


def set_frame_range(script_node,
                    include_handles=True):

    frame_start = script_node.context().get("ayon:frame_start")
    frame_end = script_node.context().get("ayon:frame_end")
    handle_start = script_node.context().get("ayon:handle_start")
    handle_end = script_node.context().get("ayon:handle_end")

    if include_handles:
        frame_start -= handle_start
        frame_end += handle_end
    log.info(f"Setting frame range: [{frame_start}-{frame_end}")
    script_node["frameRange"]["start"].setValue(int(frame_start))
    script_node["frameRange"]["end"].setValue(int(frame_end))

    # if we are in a GUI session we also reset the frame slider
    application = script_node.ancestor(Gaffer.ApplicationRoot)
    if application:
        import GafferUI
        playback = GafferUI.Playback.acquire(script_node.context())
        playback.setFrameRange(frame_start, frame_end)


def replace_node(old_node, new_node, ignore_plug_names=[], rename=True):
    all_plugs = []
    get_all_plugs(old_node, all_plugs)
    ignore_plug_names += ["globals"]
    plug_data = {}
    for plug in all_plugs:
        if plug.getName() in ignore_plug_names:
            continue

        if not bool(plug.getFlags() & Gaffer.Plug.Flags.Serialisable):
            log.debug(f"Throwing out non-serializable {plug.getName()}")
            continue
        try:
            plug_data[plug] = plug.getValue()
        except Exception:
            pass

    # first store old connections
    with Gaffer.UndoScope(old_node.scriptNode()):
        connections = get_node_connections(old_node)
        for src, pluginfo in connections.items():
            source_plug = new_node
            for part in src.split('.'):
                if source_plug is None:
                    continue
                source_plug = source_plug.getChild(part)

            for plug in pluginfo['in']:
                if source_plug is None:
                    log.debug(f"! {src} is None")
                    continue
                source_plug.setInput(plug)
            for plug in pluginfo['out']:
                plug.setInput(source_plug)

        for plug, value in plug_data.items():
            plug_relative_name = plug.fullName().replace(
                plug.node().fullName(), "")[1:]  # strip out the prefix .

            target_plug = new_node
            for part in plug_relative_name.split("."):
                try:
                    target_plug = target_plug[part]
                except KeyError:
                    target_plug = None
                    break

            if target_plug is None:
                # the target plug does not exist. we need to create it
                copy_plug(plug, new_node)
            else:
                if not bool(target_plug.getFlags() &
                            Gaffer.Plug.Flags.Serialisable):
                    log.debug(f"Skipping non-serializable plug {target_plug}")
                    continue
                try:
                    # log.debug(f"Setting {target_plug.fullName()} to {value}")
                    target_plug.setValue(value)
                except Exception as err:
                    log.debug(f"Error setting [{target_plug}]={value}: {err}")
        old_name = old_node.getName()
        new_node.scriptNode().removeChild(old_node)
        new_node.setName(old_name)
        # and finally we have a hack to avoid `scene:path` errors on some
        # upstream nodes after replacing a node
        for n in Gaffer.NodeAlgo.upstreamNodes(new_node):
            try:
                before = n["enabled"].getValue()
                n["enabled"].setValue(False)
                n["enabled"].setValue(True)
                n["enabled"].setValue(before)
            except Exception:
                pass


def copy_plug(plug, destination_node):
    log.debug(f"Copying plug [{plug}] to {destination_node}")
    try:
        src_node = plug.node()

        plug_path = plug.fullName().replace(src_node.fullName(), "").strip(".")
        plug_parts = plug_path.split('.')[:-1]
        new_plug_parent = destination_node
        for part in plug_parts:
            new_plug_parent = new_plug_parent[part]

        new_plug = type(plug)(
            plug.getName(),
            defaultValue=plug.defaultValue(),
            flags=plug.getFlags()
        )
        new_plug_parent.addChild(new_plug)

        metadata_keys = Gaffer.Metadata.registeredValues(plug)
        for key in metadata_keys:
            value = Gaffer.Metadata.value(plug, key)
            log.debug(f"Copying metadata {key}:{value} to {new_plug}")
            Gaffer.Metadata.registerValue(new_plug, key, value)
    except Exception as err:
        log.error(f"Could not copy plug: {plug.getName()} to"
                  f"{destination_node}: {err}")


def get_all_plugs(in_node, thelist, include_non_serializable=True):
    for plug in in_node.children(Gaffer.Plug):
        if (not include_non_serializable and
                not bool(plug.getFlags() & Gaffer.Plug.Flags.Serialisable)):
            continue
        thelist.append(plug)
        if len(plug.children(Gaffer.Plug)) > 0:
            get_all_plugs(plug, thelist, include_non_serializable)


def get_plug_tree(in_node, include_non_serializable=False):
    plugs = {}

    def plug_traversal(in_node, plug_dict, include_non_serializable):
        for plug in in_node.children(Gaffer.Plug):
            if not include_non_serializable and not bool(plug.getFlags() & Gaffer.Plug.Flags.Serialisable):
                continue

            if plug not in plug_dict.keys():
                plug_dict[plug] = {}

            if len(plug.children(Gaffer.Plug)) > 0:
                plug_traversal(plug, plug_dict[plug], include_non_serializable)

    plug_traversal(in_node, plugs, include_non_serializable)
    return plugs


def get_node_connections(node, include_non_serializable=False):
    '''
    Returns a dictionary of all plugs connected to node `node`, example:

    it is of the form
    {
        'parameter relative path to `node`': {
            'in': [list of input plugs]
            'out': [list of output plugs]
        }
    }

    {
        'out.lensdistort_path': # fullName for plug on input node
        {
            'in': [], # list of plugs that 'out.lensdistort_path' has as inputs
            'out': [  # list of plugs that 'out.lensdistort_path' has as outputs
                Gaffer.StringPlug( "fileName", defaultValue = '', substitutions = IECore.StringAlgo.Substitutions.VariableSubstitutions | IECore.StringAlgo.Substitutions.EscapeSubstitutions | IECore.StringAlgo.Substitutions.TildeSubstitutions )
            ]
        },
        'out.render_path':
        {
            'in': [],
            'out': [
                Gaffer.StringPlug( "text", defaultValue = 'Hello World', )
            ]
        }
    }

    '''

    all_the_plugs = []
    get_all_plugs(node, all_the_plugs)
    plugs = {}
    for plug in all_the_plugs:
        the_input = plug.getInput()
        outputs = plug.outputs()
        if not include_non_serializable and not bool(plug.getFlags() & Gaffer.Plug.Flags.Serialisable):
            continue

        plugmap = {'in': [], 'out': []}
        if the_input is not None:
            if not node.isAncestorOf(the_input):
                plugmap['in'].append(the_input)
        for o in outputs:
            if o.node() == node or node.isAncestorOf(o):
                continue
            plugmap['out'].append(o)
        if the_input is None and len(outputs) == 0:
            continue
        if len(plugmap["in"]) == len(plugmap["out"]) == 0:
            continue
        plugs[plug.relativeName(node)] = plugmap
    return plugs


def create_render_shot_plug():
    render_shot_plug = Gaffer.NameValuePlug(
        "render:shot",
        Gaffer.StringPlug(
            "value",
            defaultValue='',
            flags=Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic
        ),
        True,
        "render:shot",
        Gaffer.Plug.Flags.Default | Gaffer.Plug.Flags.Dynamic)
    return render_shot_plug


def create_multishot_context_vars(script_node):
    context_vars = script_node["variables"]
    existing_variables = [var["name"].getValue()
                          for var in context_vars.children()]
    if "render:shot" not in existing_variables:
        render_shot_plug = create_render_shot_plug()
        context_vars.addChild(render_shot_plug)


def node_name_from_template(template_string, context):
    try:
        from ayon_core.pipeline.template_data import (
            construct_folder_full_name
        )
        use_full_name = True
    except ModuleNotFoundError:
        # couldn't load the rvx custom core function
        use_full_name = False
    folder_entity = context["folder"]
    product_name = context["product"]["name"]
    folder_entity = context["folder"]
    hierarchy_parts = folder_entity["path"].split("/")
    hierarchy_parts.pop(0)
    hierarchy_parts.pop(-1)
    if use_full_name:
        full_name = construct_folder_full_name(
            context["project"]["name"], folder_entity, hierarchy_parts)
    else:
        full_name = folder_entity["name"]
    product_entity = context["product"]
    product_name = product_entity["name"]
    product_type = product_entity["productType"]
    repre_entity = context["representation"]
    repre_cont = repre_entity["context"]
    formatting_data = {
        "asset_name": folder_entity["name"],
        "asset_type": "asset",
        "folder": {
            "name": folder_entity["name"],
            "fullname": full_name,
        },
        "subset": product_name,
        "product": {
            "name": product_name,
            "type": product_type,
        },
        "family": product_type,
        "ext": repre_cont["representation"],
    }
    template = ayon_core.lib.StringTemplate(template_string)
    return template.format(formatting_data)


def append_to_csv_plug(plug, value_to_add, allow_duplicates=False):
    '''
    csv in this case is comma-separated-value
    some plugs, like deadline's limit parameter expects a comma separated list
    of things. This utility function makes adding to such lists easier
    '''

    current_value = plug.getValue()
    value_list = current_value.split(',')
    if allow_duplicates:
        value_list.append(value_to_add)
    else:
        if value_to_add not in value_list:
            value_list.append(value_to_add)
    plug.setValue(",".join(value_list))


def traverse_nodegraph(root_node: Gaffer.Node, result: list):
    children = root_node.children(Gaffer.Node)
    if len(children) == 0:
        return
    for child in children:
        result.append(child)
        traverse_nodegraph(child, result)


def get_all_children(root_node: Gaffer.Node):
    """
    Return a list of all the nodes that are children of `root_node` so if
    the script node is passed return all nodes in the script.

    """
    all_nodes = []
    traverse_nodegraph(root_node, all_nodes)
    return all_nodes
