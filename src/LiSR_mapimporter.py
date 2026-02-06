bl_info = {
    "name": "LiS:R Map Importer",
    "author": "ZeoNyph",
    "version": (1, 4, 0),
    "blender": (5, 0, 0),
    "category": "Object",
    "description": r"Addon that imports .umap files from Life is Strange: Remastered into Blender",
}

import bpy
import json
import mathutils
from mathutils import Euler
import math
import os
from collections import defaultdict


def build_file_index(root_dir, extensions=('.mat', '.tga', '.props.txt')):
    """Build filename -> path mapping once for fast lookups."""
    index = {}
    for subdir, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(extensions):
                index[file] = os.path.join(subdir, file)
    return index


def build_animation_index(root_dir):
    """Build animation name -> PSA file path mapping."""
    index = {}
    for subdir, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith('.psa'):
                # Map filename without extension to full path
                name = file[:-4]  # Remove .psa extension
                index[name] = os.path.join(subdir, file)
    return index


def build_audio_index(root_dir):
    """Build audio name -> audio file path mapping for WWise audio.

    WWise audio files are named by numeric ID (e.g., 123456789.wav).
    The .txt translation tables map IDs to sound names.
    This function builds a mapping from sound names to file paths.
    """
    import glob
    import re

    index = {}

    # Find WwiseAudio directory
    wwise_dir = os.path.join(root_dir, "LiS", "Content", "WwiseAudio", "Windows")
    if not os.path.exists(wwise_dir):
        wwise_dir = os.path.join(root_dir, "LiS", "Content", "WwiseAudio")
    if not os.path.exists(wwise_dir):
        wwise_dir = os.path.join(root_dir, "WwiseAudio")
    if not os.path.exists(wwise_dir):
        return index

    # Step 1: Build ID -> file path mapping from converted audio files
    id_to_file = {}
    for ext in ('*.wav', '*.ogg'):
        for audio_file in glob.glob(os.path.join(wwise_dir, "**", ext), recursive=True):
            basename = os.path.splitext(os.path.basename(audio_file))[0]
            # Check if filename is numeric (WWise ID)
            if basename.isdigit():
                id_to_file[basename] = audio_file
            else:
                # Non-numeric name - add directly to index
                index[basename] = audio_file

    # Step 2: Parse .txt translation tables to get ID -> name mapping
    id_to_name = {}
    for txt_file in glob.glob(os.path.join(wwise_dir, "*.txt")):
        try:
            # Try UTF-8 first, fall back to latin-1 for files with special characters
            content = None
            for encoding in ('utf-8', 'latin-1', 'cp1252'):
                try:
                    with open(txt_file, 'r', encoding=encoding) as f:
                        content = f.read()
                    break
                except UnicodeDecodeError:
                    continue

            if not content:
                continue

            in_memory_audio_section = False
            for line in content.split('\n'):
                # Check for section headers
                if line.startswith("In Memory Audio"):
                    in_memory_audio_section = True
                    continue
                elif line.startswith(("Event\t", "Switch Group\t", "Switch\t", "State Group\t")):
                    in_memory_audio_section = False
                    continue

                if in_memory_audio_section and '\t' in line:
                    parts = line.strip().split('\t')
                    if len(parts) >= 2:
                        audio_id = parts[0].strip()
                        audio_name = parts[1].strip()
                        if audio_id.isdigit() and audio_name:
                            id_to_name[audio_id] = audio_name
        except Exception as e:
            print(f"Warning: Failed to parse {os.path.basename(txt_file)}: {e}")

    # Step 3: Map sound names to file paths using the ID mapping
    for audio_id, audio_name in id_to_name.items():
        if audio_id in id_to_file:
            # Map both the full name and potential variations
            index[audio_name] = id_to_file[audio_id]
            # Also try with common prefixes stripped/added
            if audio_name.startswith("A_"):
                index[audio_name[2:]] = id_to_file[audio_id]

    print(f"  Audio index: {len(id_to_file)} files, {len(id_to_name)} name mappings, {len(index)} indexed")
    return index


def build_component_lookup(json_entities):
    """Build Outer -> entity mapping for finding SceneComponents by parent actor."""
    lookup = {}
    for entity in json_entities:
        entity_type = entity.get("Type", "")
        outer = entity.get("Outer", "")
        name = entity.get("Name", "")

        # Map SceneComponents by their Outer (parent actor name)
        # e.g., SceneComponent with Outer="SD3DSound_0" -> lookup["SD3DSound_0"] = entity
        if entity_type == "SceneComponent" and outer:
            # Store the component, keyed by its parent actor name
            lookup[outer] = entity

    return lookup


def build_anim_actor_mapping(json_entities):
    """Build mapping: InterpGroup -> SkeletalMesh name.

    Traces the relationship chain:
    1. MatineeActor.GroupActorInfos -> InterpGroup -> SkeletalMeshActorMAT
    2. SkeletalMeshComponent (with Outer=actor name) -> SkeletalMesh name
    """
    import re

    # Step 1: Build InterpGroup -> SkeletalMeshActorMAT name mapping from MatineeActors
    group_to_actor = {}
    for entity in json_entities:
        if entity.get("Type") != "MatineeActor":
            continue
        props = entity.get("Properties", {})
        for info in props.get("GroupActorInfos", []):
            group_name = info.get("ObjectName", "")  # e.g., "InterpGroup_2"
            actors = info.get("Actors", [])
            if actors and group_name:
                # Find first non-None actor with SkeletalMeshActorMAT
                for actor in actors:
                    if actor is None:
                        continue
                    actor_obj_name = actor.get("ObjectName", "")
                    # Extract actor name: "SkeletalMeshActorMAT'...SkeletalMeshActorMAT_9'" -> "SkeletalMeshActorMAT_9"
                    if "SkeletalMeshActorMAT" in actor_obj_name:
                        # Handle formats like "SkeletalMeshActorMAT'E1_2A:PersistentLevel.SkeletalMeshActorMAT_9'"
                        match = re.search(r"\.(\w+)'$", actor_obj_name)
                        if match:
                            actor_name = match.group(1)
                            group_to_actor[group_name] = actor_name
                            break  # Found a valid actor, move to next group

    # Step 2: Build SkeletalMeshActorMAT name -> SkeletalMesh name mapping
    actor_to_mesh = {}
    for entity in json_entities:
        if entity.get("Type") != "SkeletalMeshComponent":
            continue
        outer = entity.get("Outer", "")  # e.g., "SkeletalMeshActorMAT_9"
        if not outer:
            continue
        props = entity.get("Properties", {})
        mesh_ref = props.get("SkeletalMesh", {})
        mesh_name = mesh_ref.get("ObjectName", "")
        if mesh_name:
            # "SkeletalMesh'CH_L_Hayden01'" -> "CH_L_Hayden01"
            match = re.search(r"SkeletalMesh'([^']+)'", mesh_name)
            if match:
                actor_to_mesh[outer] = match.group(1)

    # Step 3: Combine to get InterpGroup -> SkeletalMesh name mapping
    group_to_mesh = {}
    for group, actor in group_to_actor.items():
        if actor in actor_to_mesh:
            group_to_mesh[group] = actor_to_mesh[actor]

    return group_to_mesh


def split_object_path(object_path):
    """Split ObjectPath, removing trailing period and digit."""
    path_parts = object_path.split(".")
    if len(path_parts) > 1:
        return path_parts[0]
    return object_path


def parse_props_file(filepath):
    """Parse a .props.txt file and extract material properties."""
    import re

    result = {
        'blend_mode': 0,  # 0=Opaque, 1=Masked, 2=Translucent, 3=Additive
        'two_sided': False,
        'opacity_clip': 0.5,
        'scalar_params': {},
        'vector_params': {},
    }

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return result

    # Parse BlendMode = BLEND_X (N)
    blend_match = re.search(r'BlendMode\s*=\s*BLEND_\w+\s*\((\d+)\)', content)
    if blend_match:
        result['blend_mode'] = int(blend_match.group(1))

    # Parse TwoSided = true/false
    twosided_match = re.search(r'TwoSided\s*=\s*(true|false)', content, re.IGNORECASE)
    if twosided_match:
        result['two_sided'] = twosided_match.group(1).lower() == 'true'

    # Parse OpacityMaskClipValue = N
    opacity_match = re.search(r'OpacityMaskClipValue\s*=\s*([\d.]+)', content)
    if opacity_match:
        result['opacity_clip'] = float(opacity_match.group(1))

    # Parse ScalarParameterValues blocks
    # Pattern: ParameterInfo = { Name=X } followed by ParameterValue = N
    scalar_pattern = re.compile(
        r'ParameterInfo\s*=\s*\{\s*Name\s*=\s*(\w+)\s*\}[^}]*?ParameterValue\s*=\s*([-\d.]+)',
        re.DOTALL
    )
    for match in scalar_pattern.finditer(content):
        param_name = match.group(1)
        try:
            param_value = float(match.group(2))
            result['scalar_params'][param_name] = param_value
        except ValueError:
            pass

    # Parse VectorParameterValues for colors
    # Pattern: Value = { R=N, G=N, B=N, A=N } with Name = X
    vector_pattern = re.compile(
        r'Value\s*=\s*\{\s*R\s*=\s*([-\d.]+)\s*,\s*G\s*=\s*([-\d.]+)\s*,\s*B\s*=\s*([-\d.]+)\s*,\s*A\s*=\s*([-\d.]+)\s*\}[^}]*?Name\s*=\s*(\w+)',
        re.DOTALL
    )
    for match in vector_pattern.finditer(content):
        try:
            r = float(match.group(1))
            g = float(match.group(2))
            b = float(match.group(3))
            a = float(match.group(4))
            name = match.group(5)
            result['vector_params'][name] = (r, g, b, a)
        except ValueError:
            pass

    # Parse TextureParameterValues for texture references
    # Pattern: ParameterInfo = { Name=XXX } followed immediately by ParameterValue = Texture2D'...'
    result['texture_params'] = {}
    texture_pattern = re.compile(
        r'ParameterInfo\s*=\s*\{\s*Name\s*=\s*([^\}]+?)\s*\}'
        r'\s+'
        r"ParameterValue\s*=\s*Texture2D'[^']*?/([^/']+)\.[^']*'"
    )
    for match in texture_pattern.finditer(content):
        param_name = match.group(1).strip()
        texture_name = match.group(2).strip()
        result['texture_params'][param_name] = texture_name

    return result


def has_alpha_variation(image_path):
    """Check if an image's alpha channel has meaningful variation (not all white).

    Returns True if the alpha channel has at least 0.1 variation, indicating
    it contains roughness data rather than being blank/white.
    """
    try:
        img = bpy.data.images.load(image_path)
        pixels = list(img.pixels)
        # Sample alpha values (every 4th value starting at index 3)
        alpha_samples = pixels[3::4][:1000]  # Sample first 1000 pixels for speed
        if not alpha_samples:
            return False
        min_alpha = min(alpha_samples)
        max_alpha = max(alpha_samples)
        # If there's at least 0.1 variation, consider it valid roughness data
        return (max_alpha - min_alpha) > 0.1
    except Exception:
        return False


class MIAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    base_directory: bpy.props.StringProperty(
        name="Assets Base Directory",
        description="Directory that contains all the unpacked assets (directory within must follow this path structure: LiS/Content/(your exported directories and files))",
        default="",
        subtype="DIR_PATH",
    )

    json_file: bpy.props.StringProperty(
        name=".umap JSON File",
        description="The exported JSON file to import into Blender",
        default="",
        subtype="FILE_PATH",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "base_directory")
        layout.prop(self, "json_file")


class CompletedFileItem(bpy.types.PropertyGroup):
    """Property group for completed file tracking."""
    name: bpy.props.StringProperty(name="Name")
    entity_count: bpy.props.IntProperty(name="Entity Count")


class VIEW3D_PT_map_importer_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "LiS:R Map Importer"
    bl_label = "LiS:R Map Importer"

    def draw_header(self, context):
        pass

    def draw(self, context):
        prefs = context.preferences.addons[__name__].preferences
        wm = context.window_manager
        layout = self.layout

        # Version in header row
        row = layout.row()
        row.alignment = 'RIGHT'
        row.label(text="v1.4")

        # ─── ASSETS SECTION ───
        assets_box = layout.box()
        assets_header = assets_box.row()
        assets_header.label(text="ASSETS", icon='FILE_FOLDER')

        assets_box.prop(prefs, "base_directory", text="")
        help_row = assets_box.row()
        help_row.scale_y = 0.7
        help_row.label(text="Path to exported game assets", icon='INFO')

        layout.separator(factor=0.5)

        # ─── OPTIONS SECTION ───
        options_box = layout.box()
        options_header = options_box.row()
        options_header.label(text="Options", icon='OPTIONS')

        # Import toggles row
        import_row = options_box.row()
        import_row.label(text="Import:")
        import_row.prop(wm, "lisr_import_meshes", text="Meshes", toggle=True)
        import_row.prop(wm, "lisr_import_lights", text="Lights", toggle=True)
        import_row.prop(wm, "lisr_import_animations", text="Animations", toggle=True)
        import_row.prop(wm, "lisr_import_sounds", text="Sounds", toggle=True)


        # Scale factor row
        scale_row = options_box.row()
        scale_row.label(text="Scale:")
        scale_row.prop(wm, "lisr_scale_factor", text="")

        layout.separator(factor=0.5)

        # ─── IMPORT BUTTON ───
        import_row = layout.row()
        import_row.scale_y = 2.0

        # Disable button if no assets path set
        has_path = bool(prefs.base_directory and prefs.base_directory.strip())
        import_row.enabled = has_path and not wm.lisr_import_running
        import_row.operator(BulkMapImporter.bl_idname, text="Import", icon='IMPORT')

        if not has_path:
            hint_row = layout.row()
            hint_row.scale_y = 0.7
            hint_row.label(text="Set assets path to enable import", icon='ERROR')

        # ─── PROGRESS SECTION ─── (only visible during import)
        if wm.lisr_import_running:
            layout.separator(factor=0.5)
            progress_box = layout.box()
            progress_header = progress_box.row()
            progress_header.label(text="Progress", icon='TIME')

            # Progress bar
            progress_box.prop(wm, "lisr_import_progress", text="")

            # Current file info
            if wm.lisr_current_file:
                file_row = progress_box.row()
                file_row.label(text=wm.lisr_current_file, icon='FILE')

                if wm.lisr_entity_total > 0:
                    entity_row = progress_box.row()
                    entity_row.label(
                        text=f"Processing {wm.lisr_entity_current:,} of {wm.lisr_entity_total:,} entities"
                    )

            # File queue counter
            if wm.lisr_queue_total > 1:
                progress_box.separator(factor=0.3)
                queue_row = progress_box.row()
                queue_row.label(
                    text=f"File {wm.lisr_queue_index + 1} of {wm.lisr_queue_total}",
                    icon='PACKAGE'
                )

            # Completed files list
            if len(wm.lisr_completed_files) > 0:
                progress_box.separator(factor=0.3)
                for completed in wm.lisr_completed_files:
                    done_row = progress_box.row()
                    done_row.label(
                        text=f"{completed.name} ({completed.entity_count:,} entities)",
                        icon='CHECKMARK'
                    )

        # ─── STATUS SECTION ─── (after import complete)
        elif wm.lisr_import_complete:
            layout.separator(factor=0.5)
            status_box = layout.box()

            status_header = status_box.row()
            status_header.label(text="STATUS", icon='CHECKMARK')

            result_row = status_box.row()
            result_row.label(text="Import complete", icon='FILE_TICK')

            stats_row = status_box.row()
            maps_count = wm.lisr_total_maps
            objects_count = wm.lisr_total_objects
            materials_count = wm.lisr_total_materials
            stats_row.label(
                text=f"{maps_count} maps • {objects_count:,} objects • {materials_count:,} materials"
            )


class StaticMesh:
    """Represents a static mesh entity to be imported."""

    def __init__(self, json_entity, base_dir, asset_sub_dir='', scale_factor=1.0):
        self.entity_name = json_entity.get("Outer", 'Error')
        self.import_path = ""
        self.pos = [0, 0, 0]
        self.rot = [0, 0, 0]
        self.scale = [1, 1, 1]
        self.invalid = False
        self.skip_reason = ""
        self._scale_factor = scale_factor

        props = json_entity.get("Properties", None)
        if not props:
            self.invalid = True
            self.skip_reason = "no properties"
            return

        if not props.get("StaticMesh", None):
            self.invalid = True
            self.skip_reason = "no static mesh"
            return

        object_path = props.get("StaticMesh").get("ObjectPath", None)

        if not object_path or object_path == '':
            self.invalid = True
            self.skip_reason = "no object path"
            return

        if 'BasicShapes' in object_path:
            self.invalid = True
            self.skip_reason = "basic shape"
            return

        objpath = split_object_path(object_path)
        self.import_path = base_dir + asset_sub_dir + objpath + ".gltf"

        if not os.path.exists(self.import_path):
            self.invalid = True
            self.skip_reason = "file not found"
            return

        # Apply scale factor to positions
        if props.get("RelativeLocation", False):
            pos = props.get("RelativeLocation")
            self.pos = [
                pos.get("X") / 100 * scale_factor,
                pos.get("Y") / -100 * scale_factor,
                pos.get("Z") / 100 * scale_factor
            ]

        if props.get("RelativeRotation", False):
            rot = props.get("RelativeRotation")
            self.rot = [rot.get("Roll"), rot.get("Pitch")*-1, rot.get("Yaw")*-1]

        if props.get("RelativeScale3D", False):
            scale = props.get("RelativeScale3D")
            self.scale = [
                scale.get("X", 1) * scale_factor,
                scale.get("Y", 1) * scale_factor,
                scale.get("Z", 1) * scale_factor
            ]
        else:
            self.scale = [scale_factor, scale_factor, scale_factor]


class SkeletalMesh:
    """Represents a skeletal mesh entity to be imported."""

    def __init__(self, json_entity, base_dir, asset_sub_dir='', scale_factor=1.0):
        self.entity_name = json_entity.get("Outer", 'Error')
        self.import_path = ""
        self.pos = [0, 0, 0]
        self.rot = [0, 0, 0]
        self.scale = [1, 1, 1]
        self.invalid = False
        self.skip_reason = ""
        self._scale_factor = scale_factor
        self.anim_sequences = []  # List of animation sequence names

        props = json_entity.get("Properties", None)
        if not props:
            self.invalid = True
            self.skip_reason = "no properties"
            return

        if not props.get("SkeletalMesh", None):
            self.invalid = True
            self.skip_reason = "no skeletal mesh"
            return

        object_path = props.get("SkeletalMesh").get("ObjectPath", None)

        if not object_path or object_path == '':
            self.invalid = True
            self.skip_reason = "no object path"
            return

        objpath = split_object_path(object_path)
        self.import_path = base_dir + asset_sub_dir + objpath + ".gltf"

        if not os.path.exists(self.import_path):
            self.invalid = True
            self.skip_reason = "file not found"
            return

        # Extract animation references
        self._extract_anim_references(props)

        # Apply scale factor to positions
        if props.get("RelativeLocation", False):
            pos = props.get("RelativeLocation")
            self.pos = [
                pos.get("X") / 100 * scale_factor,
                pos.get("Y") / -100 * scale_factor,
                pos.get("Z") / 100 * scale_factor
            ]

        if props.get("RelativeRotation", False):
            rot = props.get("RelativeRotation")
            self.rot = [rot.get("Roll"), rot.get("Pitch")*-1, rot.get("Yaw")*-1]

        if props.get("RelativeScale3D", False):
            scale = props.get("RelativeScale3D")
            self.scale = [
                scale.get("X", 1) * scale_factor,
                scale.get("Y", 1) * scale_factor,
                scale.get("Z", 1) * scale_factor
            ]
        else:
            self.scale = [scale_factor, scale_factor, scale_factor]

    def _extract_anim_references(self, props):
        """Extract animation sequence references from properties."""
        import re

        # Pattern to extract animation name from ObjectName
        anim_pattern = re.compile(r"AnimSequence'([^']+)'")

        def extract_anim_name(obj):
            """Recursively extract animation names from a property object."""
            if isinstance(obj, dict):
                # Check for ObjectName with AnimSequence
                obj_name = obj.get("ObjectName", "")
                if "AnimSequence'" in obj_name:
                    match = anim_pattern.search(obj_name)
                    if match:
                        self.anim_sequences.append(match.group(1))
                # Recurse into dict values
                for value in obj.values():
                    extract_anim_name(value)
            elif isinstance(obj, list):
                for item in obj:
                    extract_anim_name(item)

        # Search through all properties for animation references
        extract_anim_name(props)


class GameLight:
    """Represents a light entity to be imported."""

    def __init__(self, json_entity):
        self.entity_name = json_entity.get("Outer", 'Error')
        self.type = json_entity.get("Type", "SpotLightComponent")
        self.pos = [0, 0, 0]
        self.rot = [0, 0, 0]
        self.scale = [1, 1, 1]
        self.invalid = False

        props = json_entity.get("Properties", None)
        if not props:
            self.invalid = True
            return

        if props.get("RelativeLocation", False):
            pos = props.get("RelativeLocation")
            self.pos = [pos.get("X")/100, pos.get("Y")/-100, pos.get("Z")/100]

        if props.get("RelativeRotation", False):
            rot = props.get("RelativeRotation")
            self.rot = [rot.get("Roll"), rot.get("Pitch")*-1, rot.get("Yaw")*-1]

        if props.get("RelativeScale3D", False):
            scale = props.get("RelativeScale3D")
            self.scale = [scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1)]

    def import_light(self, collection):
        if self.invalid:
            return None

        if self.type == 'SpotLightComponent':
            light_data = bpy.data.lights.new(name=self.entity_name, type='SPOT')
        elif self.type == 'PointLightComponent':
            light_data = bpy.data.lights.new(name=self.entity_name, type='POINT')
        else:
            light_data = bpy.data.lights.new(name=self.entity_name, type='POINT')

        light_obj = bpy.data.objects.new(name=self.entity_name, object_data=light_data)
        light_obj.scale = (self.scale[0], self.scale[1], self.scale[2])
        light_obj.location = (self.pos[0], self.pos[1], self.pos[2])
        light_obj.rotation_mode = 'XYZ'
        light_obj.rotation_euler = Euler((math.radians(self.rot[0]), math.radians(self.rot[1]), math.radians(self.rot[2])), 'XYZ')
        collection.objects.link(light_obj)
        return light_obj


class GameSound:
    """Represents a 3D sound entity to be imported."""

    def __init__(self, json_entity, component_lookup, scale_factor=1.0):
        self.entity_name = json_entity.get("Name", 'Error')
        self.pos = [0, 0, 0]
        self.rot = [0, 0, 0]
        self.audio_id = ""
        self.ak_event_path = ""
        self.inner_radius = 200.0
        self.outer_radius = 1000.0
        self.invalid = False

        props = json_entity.get("Properties", {})
        self.audio_id = props.get("Audio_ID", "")
        self.inner_radius = props.get("InnerRadius", 200.0)
        self.outer_radius = props.get("OuterRadius", 1000.0)

        ak_event = props.get("AkEvent", {})
        self.ak_event_path = ak_event.get("ObjectPath", "")

        # Get position from SceneComponent whose Outer matches this sound's Name
        # e.g., SD3DSound_0 -> lookup for SceneComponent with Outer="SD3DSound_0"
        if self.entity_name in component_lookup:
            comp = component_lookup[self.entity_name]
            comp_props = comp.get("Properties", {})
            loc = comp_props.get("RelativeLocation", {})
            self.pos = [
                loc.get("X", 0) / 100 * scale_factor,
                loc.get("Y", 0) / -100 * scale_factor,
                loc.get("Z", 0) / 100 * scale_factor
            ]


class BulkMapImporter(bpy.types.Operator):
    """Open file browser to select multiple .umap JSON files for import."""
    bl_idname = "lis.bulk_map_import"
    bl_label = "Import Maps"
    bl_options = {'REGISTER', 'UNDO'}

    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        wm = context.window_manager

        # Reset all tracking properties
        wm.lisr_import_complete = False
        wm.lisr_total_objects = 0
        wm.lisr_total_materials = 0
        wm.lisr_total_maps = 0
        wm.lisr_current_file = ""
        wm.lisr_entity_current = 0
        wm.lisr_entity_total = 0
        wm.lisr_completed_files.clear()
        wm.lisr_parent_collection = ""

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        wm = context.window_manager

        if not self.files:
            self.report({'ERROR'}, "No files selected")
            return {'CANCELLED'}

        # Build list of full file paths
        file_paths = []
        for file_elem in self.files:
            full_path = os.path.join(self.directory, file_elem.name)
            if os.path.exists(full_path) and full_path.endswith('.json'):
                file_paths.append(full_path)

        if not file_paths:
            self.report({'ERROR'}, "No valid JSON files selected")
            return {'CANCELLED'}

        # Initialize queue
        wm.lisr_import_queue.clear()
        for path in file_paths:
            item = wm.lisr_import_queue.add()
            item.path = path

        wm.lisr_queue_index = 0
        wm.lisr_queue_total = len(file_paths)
        wm.lisr_total_maps = len(file_paths)

        # Create parent collection using prefix from first file (e.g., "E1_3A" from "E1_3A_MapName.json")
        first_filename = os.path.basename(file_paths[0]).replace('.json', '')
        parts = first_filename.split('_')
        if len(parts) >= 2:
            parent_name = f"{parts[0]}_{parts[1]}"
        else:
            parent_name = first_filename

        # Ensure unique collection name
        base_name = parent_name
        counter = 1
        while parent_name in bpy.data.collections:
            parent_name = f"{base_name}.{counter:03d}"
            counter += 1

        parent_collection = bpy.data.collections.new(parent_name)
        context.scene.collection.children.link(parent_collection)
        wm.lisr_parent_collection = parent_name

        self.report({'INFO'}, f"Starting import of {len(file_paths)} files...")

        # Start importing the first file
        bpy.ops.lis.map_import('INVOKE_DEFAULT', json_path=file_paths[0])

        return {'FINISHED'}


class MapImporter(bpy.types.Operator):
    """Modal operator that imports map with progress updates."""
    bl_idname = "lis.map_import"
    bl_label = "Map Importer"
    bl_options = {'REGISTER', 'UNDO'}

    json_path: bpy.props.StringProperty(
        name="JSON Path",
        description="Path to the JSON file to import (optional, uses preferences if empty)",
        default=""
    )

    _timer = None
    _entities = []
    _index = 0
    _total = 0
    _mesh_cache = {}
    _collection = None
    _collection_name = ""
    _base_dir = ""
    _import_static = True
    _import_lights = True
    _import_animations = False
    _import_sounds = False
    _scale_factor = 1.0
    _static_mesh_types = ['StaticMeshComponent', "InstancedStaticMeshComponent"]
    _skeletal_mesh_types = ['SkeletalMeshComponent']
    _light_types = ['SpotLightComponent', 'AnimatedLightComponent', 'PointLightComponent']
    _sound_types = ['SD3DSound']
    _batch_size = 5
    _objects_imported = 0
    _json_filename = ""
    _animation_index = {}
    _animations_imported = 0
    _audio_index = {}
    _component_lookup = {}
    _anim_track_types = ['InterpTrackAnimControl']
    _pending_animations = []  # List of animation info dicts to import
    _mesh_to_armature = {}  # Maps SkeletalMesh name -> armature object
    _group_to_mesh = {}  # Maps InterpGroup name -> SkeletalMesh name

    def invoke(self, context, event):
        prefs = context.preferences.addons[__name__].preferences
        wm = context.window_manager

        self._base_dir = prefs.base_directory

        # Read import options from window manager
        self._import_static = wm.lisr_import_meshes
        self._import_lights = wm.lisr_import_lights
        self._import_animations = wm.lisr_import_animations
        self._import_sounds = wm.lisr_import_sounds
        self._scale_factor = wm.lisr_scale_factor

        # Build animation index if needed
        self._animation_index = {}
        self._animations_imported = 0
        if self._import_animations:
            print("Building animation index...")
            self._animation_index = build_animation_index(self._base_dir)
            print(f"Indexed {len(self._animation_index)} PSA files")

        # Build audio index if sounds enabled
        self._audio_index = {}
        if self._import_sounds:
            print("Building audio index...")
            self._audio_index = build_audio_index(self._base_dir)
            print(f"Indexed {len(self._audio_index)} audio files")

        # Use provided json_path or fall back to preferences
        map_json_path = self.json_path if self.json_path else prefs.json_file

        if not os.path.exists(map_json_path):
            self.report({'ERROR'}, f"JSON file not found: {map_json_path}")
            return {'CANCELLED'}

        # Load and parse JSON
        with open(map_json_path) as file:
            json_object = json.load(file)

        # Store filename for tracking
        self._json_filename = os.path.basename(map_json_path)

        # Build component lookup for sound entity position resolution
        self._component_lookup = {}
        if self._import_sounds:
            self._component_lookup = build_component_lookup(json_object)

        # Collect animations from InterpTrackAnimControl entities
        self._pending_animations = []
        self._mesh_to_armature = {}
        self._group_to_mesh = {}
        if self._import_animations:
            self._group_to_mesh = build_anim_actor_mapping(json_object)
            self._pending_animations = self._collect_anim_tracks(json_object)
            if self._pending_animations:
                print(f"Found {len(self._pending_animations)} animations in sequence tracks")
            if self._group_to_mesh:
                print(f"Built animation mapping for {len(self._group_to_mesh)} InterpGroups")

        # Filter to only importable entities
        self._entities = []
        for entity in json_object:
            entity_type = entity.get('Type', None)
            if not entity_type:
                continue
            if self._import_static and entity_type in self._static_mesh_types:
                self._entities.append(('mesh', entity))
            elif self._import_static and entity_type in self._skeletal_mesh_types:
                self._entities.append(('skeletal', entity))
            elif self._import_lights and entity_type in self._light_types:
                self._entities.append(('light', entity))
            elif self._import_sounds and entity_type in self._sound_types:
                self._entities.append(('sound', entity))

        self._total = len(self._entities)
        self._index = 0
        self._mesh_cache = {}
        self._objects_imported = 0

        # Update window manager tracking
        wm.lisr_current_file = self._json_filename
        wm.lisr_entity_current = 0
        wm.lisr_entity_total = self._total

        if self._total == 0:
            self.report({'WARNING'}, "No importable entities found in JSON")
            # Check if there are more files in queue
            self._process_next_in_queue(context)
            return {'CANCELLED'}

        # Create collection and link to parent (or scene if no parent)
        self._collection_name = self._json_filename.replace('.json', '')
        self._collection = bpy.data.collections.new(self._json_filename.replace('.json', ''))

        # Link to parent collection if exists, otherwise to scene
        if wm.lisr_parent_collection and wm.lisr_parent_collection in bpy.data.collections:
            parent = bpy.data.collections[wm.lisr_parent_collection]
            parent.children.link(self._collection)
        else:
            context.scene.collection.children.link(self._collection)

        # Setup progress
        wm.lisr_import_running = True
        wm.lisr_import_progress = 0.0

        # Start timer
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)

        self.report({'INFO'}, f"Starting import of {self._total} entities from {self._json_filename}...")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        wm = context.window_manager

        if event.type == 'ESC':
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            # Process a batch of entities
            end_index = min(self._index + self._batch_size, self._total)

            for i in range(self._index, end_index):
                entity_type, entity = self._entities[i]

                if entity_type == 'mesh':
                    obj = self._import_mesh_entity(entity)
                    if obj:
                        self._objects_imported += 1
                elif entity_type == 'skeletal':
                    obj = self._import_skeletal_entity(entity)
                    if obj:
                        self._objects_imported += 1
                elif entity_type == 'light':
                    light = GameLight(entity)
                    light_obj = light.import_light(self._collection)
                    if light_obj:
                        self._objects_imported += 1
                elif entity_type == 'sound':
                    speaker_obj = self._import_sound_entity(entity)
                    if speaker_obj:
                        self._objects_imported += 1

            self._index = end_index

            # Update progress tracking
            wm.lisr_entity_current = self._index

            # Calculate overall progress across all files
            if wm.lisr_queue_total > 0:
                file_progress = self._index / self._total if self._total > 0 else 1.0
                overall_progress = (wm.lisr_queue_index + file_progress) / wm.lisr_queue_total
                wm.lisr_import_progress = overall_progress * 100
            else:
                progress = self._index / self._total if self._total > 0 else 1.0
                wm.lisr_import_progress = progress * 100

            # Force UI update
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

            # Check if done
            if self._index >= self._total:
                self.finish(context)
                return {'FINISHED'}

        return {'PASS_THROUGH'}

    def _import_mesh_entity(self, entity):
        """Import a mesh entity, using cache for instancing."""
        static_mesh = StaticMesh(entity, self._base_dir, scale_factor=self._scale_factor)

        if static_mesh.invalid:
            return None

        path = static_mesh.import_path

        if path in self._mesh_cache:
            # Create linked instance
            cached_data = self._mesh_cache[path]
            new_obj = bpy.data.objects.new(static_mesh.entity_name, cached_data)
        else:
            # Import fresh
            bpy.ops.import_scene.gltf(filepath=path)
            imported_obj = bpy.context.object

            if imported_obj is None:
                return None

            # Cache the mesh data for future instances
            self._mesh_cache[path] = imported_obj.data
            new_obj = imported_obj
            new_obj.name = static_mesh.entity_name

        # Apply transforms
        new_obj.scale = (static_mesh.scale[0], static_mesh.scale[1], static_mesh.scale[2])
        new_obj.location = (static_mesh.pos[0], static_mesh.pos[1], static_mesh.pos[2])
        new_obj.rotation_mode = 'XYZ'
        new_obj.rotation_euler = Euler((
            math.radians(static_mesh.rot[0]),
            math.radians(static_mesh.rot[1]),
            math.radians(static_mesh.rot[2])
        ), 'XYZ')

        # Move to target collection
        for coll in new_obj.users_collection:
            coll.objects.unlink(new_obj)
        self._collection.objects.link(new_obj)

        return new_obj

    def _import_skeletal_entity(self, entity):
        """Import a skeletal mesh entity, using cache for instancing."""
        skeletal_mesh = SkeletalMesh(entity, self._base_dir, scale_factor=self._scale_factor)

        if skeletal_mesh.invalid:
            return None

        path = skeletal_mesh.import_path
        armature = None

        # Extract mesh name for animation mapping (e.g., "CH_L_Hayden01" from path)
        mesh_name = os.path.basename(path).replace('.gltf', '')

        if path in self._mesh_cache:
            # Create linked instance
            cached_data = self._mesh_cache[path]
            new_obj = bpy.data.objects.new(skeletal_mesh.entity_name, cached_data)
        else:
            # Import fresh
            bpy.ops.import_scene.gltf(filepath=path)
            imported_obj = bpy.context.object

            if imported_obj is None:
                return None

            # Cache the mesh data for future instances
            self._mesh_cache[path] = imported_obj.data
            new_obj = imported_obj
            new_obj.name = skeletal_mesh.entity_name

            # Find armature in imported hierarchy for animation import
            if self._import_animations:
                armature = self._find_armature(new_obj)
                # Track mesh name to armature mapping for sequence animations
                if armature and mesh_name:
                    self._mesh_to_armature[mesh_name] = armature
                    print(f"  Mapped mesh '{mesh_name}' to armature '{armature.name}'")

        # Apply transforms
        new_obj.scale = (skeletal_mesh.scale[0], skeletal_mesh.scale[1], skeletal_mesh.scale[2])
        new_obj.location = (skeletal_mesh.pos[0], skeletal_mesh.pos[1], skeletal_mesh.pos[2])
        new_obj.rotation_mode = 'XYZ'
        new_obj.rotation_euler = Euler((
            math.radians(skeletal_mesh.rot[0]),
            math.radians(skeletal_mesh.rot[1]),
            math.radians(skeletal_mesh.rot[2])
        ), 'XYZ')

        # Move to target collection
        for coll in new_obj.users_collection:
            coll.objects.unlink(new_obj)
        self._collection.objects.link(new_obj)

        # Import animations for this skeletal mesh
        if self._import_animations and armature:
            self._import_animations_for_armature(armature, skeletal_mesh)

        return new_obj

    def _find_armature(self, obj):
        """Find armature in object hierarchy."""
        if obj.type == 'ARMATURE':
            return obj
        # Check children
        for child in obj.children:
            armature = self._find_armature(child)
            if armature:
                return armature
        # Check if obj has an armature modifier
        if obj.type == 'MESH':
            for modifier in obj.modifiers:
                if modifier.type == 'ARMATURE' and modifier.object:
                    return modifier.object
        return None

    def _import_animations_for_armature(self, armature, skeletal_mesh):
        """Import PSA animations for an armature based on referenced AnimSequences.

        Note: Animation references are typically NOT embedded in _LD.json SkeletalMeshComponents.
        This function looks for AnimSequence references in the properties, but usually finds none.
        Animations for _LD files are handled by _import_sequence_animations() instead.
        """
        if not armature:
            return
        if not self._animation_index:
            return

        # Get animation sequences referenced in the skeletal mesh component
        anim_sequences = skeletal_mesh.anim_sequences

        # Only import animations that are explicitly referenced in the component properties
        # Don't do pattern matching - that causes issues with _LD files
        if not anim_sequences:
            return

        imported_actions = []

        for anim_name in anim_sequences:
            if anim_name in self._animation_index:
                action = self._import_psa_animation(armature, self._animation_index[anim_name], anim_name)
                if action:
                    imported_actions.append(action)
                    self._animations_imported += 1

        if imported_actions:
            print(f"Imported {len(imported_actions)} animations for {skeletal_mesh.entity_name}")

    def _import_psa_animation(self, armature, anim_path, anim_name):
        """Import a PSA animation file as a Blender Action using io_scene_psk_psa addon.

        The action is imported but NOT assigned to the armature to avoid conflicts.
        Actions are stored in bpy.data.actions for manual assignment.
        """
        if not os.path.exists(anim_path):
            return None

        # Check if action already exists
        if anim_name in bpy.data.actions:
            return bpy.data.actions[anim_name]

        try:
            # Store current selection and active object
            old_active = bpy.context.view_layer.objects.active
            old_selected = [obj for obj in bpy.context.selected_objects]

            # Store old action to restore it after import
            old_action = None
            if armature.animation_data:
                old_action = armature.animation_data.action

            # Deselect all and select only the armature
            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            bpy.context.view_layer.objects.active = armature

            # Track actions before import to find newly created ones
            actions_before = set(bpy.data.actions.keys())

            # Import PSA using io_scene_psk_psa addon (operator: psa.import_all)
            try:
                result = bpy.ops.psa.import_all(filepath=anim_path)
                if result != {'FINISHED'}:
                    print(f"  PSA import returned {result} for {anim_name}")
            except RuntimeError as e:
                print(f"  PSA import error for {anim_name}: {e}")
                return None
            except AttributeError:
                print(f"PSA import addon not available. Install io_scene_psk_psa.")
                return None

            # Find newly created actions
            actions_after = set(bpy.data.actions.keys())
            new_actions = actions_after - actions_before

            # Get the imported action
            action = None
            if new_actions:
                # Get first new action
                action = bpy.data.actions[list(new_actions)[0]]
            elif armature.animation_data and armature.animation_data.action and armature.animation_data.action != old_action:
                action = armature.animation_data.action

            # Restore the armature's previous action (don't leave new action assigned)
            if armature.animation_data:
                armature.animation_data.action = old_action

            if action and action.name != anim_name:
                # Rename action to match the expected animation name
                action.name = anim_name

            # Restore selection state
            bpy.ops.object.select_all(action='DESELECT')
            for obj in old_selected:
                if obj:
                    obj.select_set(True)
            if old_active:
                bpy.context.view_layer.objects.active = old_active

            if action:
                print(f"  Imported animation '{anim_name}' with {len(action.fcurves)} F-Curves")

            return action

        except Exception as e:
            print(f"Failed to import animation {anim_name}: {e}")
            import traceback
            traceback.print_exc()
            return None
            return None

    def _import_sound_entity(self, entity):
        """Import a sound entity as a Blender Speaker."""
        sound = GameSound(entity, self._component_lookup, self._scale_factor)
        if sound.invalid:
            return None

        # Create speaker data
        speaker_name = sound.audio_id or sound.entity_name
        speaker_data = bpy.data.speakers.new(name=speaker_name)
        speaker_data.volume = 1.0
        speaker_data.attenuation = 1.0
        speaker_data.distance_reference = sound.inner_radius / 100 * self._scale_factor
        speaker_data.distance_max = sound.outer_radius / 100 * self._scale_factor

        # Try to link audio file - check multiple possible name variations
        audio_path = None
        names_to_try = []

        # Primary: Audio_ID from entity
        if sound.audio_id:
            names_to_try.append(sound.audio_id)
            # Also try without A_ prefix if present
            if sound.audio_id.startswith("A_"):
                names_to_try.append(sound.audio_id[2:])

        # Secondary: Extract from AkEvent path
        ak_sound_name = self._extract_sound_name(sound.ak_event_path)
        if ak_sound_name:
            names_to_try.append(ak_sound_name)
            if ak_sound_name.startswith("Play_"):
                names_to_try.append(ak_sound_name[5:])
            if ak_sound_name.startswith("Play_A_"):
                names_to_try.append(ak_sound_name[7:])

        # Find first matching audio file (exact match)
        for name in names_to_try:
            if name in self._audio_index:
                audio_path = self._audio_index[name]
                break

        # Fallback: Fuzzy matching based on keywords
        if not audio_path and names_to_try:
            audio_path = self._fuzzy_match_audio(names_to_try[0])

        if audio_path:
            # Load sound if not already in bpy.data.sounds
            sound_data = bpy.data.sounds.get(os.path.basename(audio_path))
            if not sound_data:
                sound_data = bpy.data.sounds.load(audio_path)
            speaker_data.sound = sound_data
        else:
            print(f"  Audio not found for {speaker_name}, tried: {names_to_try[:3]}")

        # Create speaker object
        speaker_obj = bpy.data.objects.new(speaker_name, speaker_data)
        speaker_obj.location = (sound.pos[0], sound.pos[1], sound.pos[2])

        self._collection.objects.link(speaker_obj)
        return speaker_obj

    def _fuzzy_match_audio(self, audio_id):
        """Fuzzy match an audio ID to find the best matching audio file.

        Extracts keywords from audio_id and finds files containing those keywords.
        e.g., 'A_NextDoor_Skate' -> matches 'A_E1_S02_ClassArt_NextDoor_Skate_01'
        """
        if not audio_id or not self._audio_index:
            return None

        # Extract keywords from the audio_id (split by underscore, filter short/common words)
        keywords = [kw.lower() for kw in audio_id.split('_') if len(kw) > 1]
        # Remove common prefixes that don't help with matching
        keywords = [kw for kw in keywords if kw not in ('a', 'play', 's', 'sfx', 'amb')]

        if not keywords:
            return None

        best_match = None
        best_score = 0

        for name, path in self._audio_index.items():
            name_lower = name.lower()

            # Count how many keywords are in this filename
            score = sum(1 for kw in keywords if kw in name_lower)

            # Bonus for matching more specific/longer keywords
            if score > 0:
                # Prefer files where keywords appear as whole words (between underscores)
                name_parts = set(name_lower.split('_'))
                exact_matches = sum(1 for kw in keywords if kw in name_parts)
                score += exact_matches * 0.5

            if score > best_score:
                best_score = score
                best_match = path

        # Only accept if we matched at least half of the keywords
        min_required = max(1, len(keywords) // 2)
        if best_score >= min_required:
            return best_match

        return None

    def _extract_sound_name(self, ak_event_path):
        """Extract sound name from AkEvent ObjectPath."""
        if not ak_event_path:
            return ""
        # ObjectPath format: "LiS/Content/.../Play_A_BlowTrees.0"
        # Extract the last part before the extension
        parts = ak_event_path.split("/")
        if parts:
            name_with_ext = parts[-1]
            # Remove trailing .0 or similar extension
            name_parts = name_with_ext.split(".")
            if name_parts:
                return name_parts[0]
        return ""

    def _collect_anim_tracks(self, json_entities):
        """Collect animation references from InterpTrackAnimControl entities.

        Returns a list of dicts with animation info including the target InterpGroup.
        """
        animations = []
        seen_paths = set()

        for entity in json_entities:
            if entity.get('Type') != 'InterpTrackAnimControl':
                continue

            outer = entity.get('Outer', '')  # InterpGroup name (e.g., "InterpGroup_2")
            props = entity.get('Properties', {})
            slot_name = props.get('SlotName', '')
            anim_seqs = props.get('AnimSeqs', [])

            for anim_seq in anim_seqs:
                anim_ref = anim_seq.get('AnimSeq', {})
                obj_path = anim_ref.get('ObjectPath', '')
                obj_name = anim_ref.get('ObjectName', '')

                if not obj_path or obj_path in seen_paths:
                    continue

                seen_paths.add(obj_path)

                # Extract animation name from ObjectName like "AnimSequence'A_E1_2A_ArtClass_Alyssa_Loop_MFFat'"
                anim_name = ""
                if "AnimSequence'" in obj_name:
                    start = obj_name.find("AnimSequence'") + len("AnimSequence'")
                    end = obj_name.find("'", start)
                    if end > start:
                        anim_name = obj_name[start:end]

                if anim_name:
                    animations.append({
                        'name': anim_name,
                        'path': obj_path,
                        'group': outer,
                        'slot': slot_name
                    })

        return animations

    def _import_sequence_animations(self):
        """Import animations collected from InterpTrackAnimControl entities.

        Uses the group-to-mesh and mesh-to-armature mappings to apply
        animations to the correct armatures.
        """
        if not self._pending_animations:
            return

        imported_count = 0
        linked_count = 0

        for anim_info in self._pending_animations:
            anim_name = anim_info['name']
            obj_path = anim_info['path']
            group = anim_info['group']

            # Convert ObjectPath to file path
            # Format: "LiS/Content/Packages/Animations/AS_E1_2A/.../A_Name.0"
            # Remove trailing .0 and add .psa
            clean_path = obj_path.rsplit('.', 1)[0] if '.' in obj_path else obj_path
            psa_path = os.path.join(self._base_dir, clean_path + ".psa")

            if not os.path.exists(psa_path):
                # Try looking in animation index by name
                if anim_name in self._animation_index:
                    psa_path = self._animation_index[anim_name]
                else:
                    print(f"  Animation not found: {anim_name}")
                    continue

            # Find target armature through the mapping chain
            target_armature = None
            target_mesh = self._group_to_mesh.get(group)

            if target_mesh and target_mesh in self._mesh_to_armature:
                target_armature = self._mesh_to_armature[target_mesh]
                print(f"  Animation '{anim_name}' -> group '{group}' -> mesh '{target_mesh}' -> armature '{target_armature.name}'")

            if target_armature:
                # Import PSA directly to the target armature
                action = self._import_psa_animation(target_armature, psa_path, anim_name)
                if action:
                    imported_count += 1
                    linked_count += 1
            else:
                # No target found, import as standalone action
                action = self._import_psa_as_action(psa_path, anim_name)
                if action:
                    imported_count += 1
                    if group:
                        print(f"  Animation '{anim_name}' imported but no armature found for group '{group}'")

        if imported_count > 0:
            print(f"Imported {imported_count} sequence animations ({linked_count} linked to armatures)")

    def _import_psa_as_action(self, psa_path, anim_name):
        """Import a PSA file as a Blender Action using io_scene_psk_psa addon."""
        if not os.path.exists(psa_path):
            return None

        # Check if action already exists
        if anim_name in bpy.data.actions:
            return bpy.data.actions[anim_name]

        try:
            # We need an armature to import the PSA
            temp_armature = None
            for obj in bpy.data.objects:
                if obj.type == 'ARMATURE':
                    temp_armature = obj
                    break

            if not temp_armature:
                print(f"  No armature found to import animation '{anim_name}'")
                return None

            # Store current state
            old_active = bpy.context.view_layer.objects.active
            old_selected = [obj for obj in bpy.context.selected_objects]
            old_action = temp_armature.animation_data.action if temp_armature.animation_data else None

            # Select armature and make active
            bpy.ops.object.select_all(action='DESELECT')
            temp_armature.select_set(True)
            bpy.context.view_layer.objects.active = temp_armature

            # Track actions before import
            actions_before = set(bpy.data.actions.keys())

            # Import PSA using io_scene_psk_psa addon
            try:
                result = bpy.ops.psa.import_all(filepath=psa_path)
                if result != {'FINISHED'}:
                    print(f"  PSA import returned {result} for {anim_name}")
            except RuntimeError as e:
                print(f"  PSA import error for {anim_name}: {e}")
                return None
            except AttributeError:
                print(f"PSA import addon not available. Install io_scene_psk_psa.")
                return None

            # Find newly created actions
            actions_after = set(bpy.data.actions.keys())
            new_actions = actions_after - actions_before

            # Get the imported action
            action = None
            if new_actions:
                action = bpy.data.actions[list(new_actions)[0]]
                if action.name != anim_name:
                    action.name = anim_name
            elif temp_armature.animation_data and temp_armature.animation_data.action:
                action = temp_armature.animation_data.action
                if action.name != anim_name:
                    action.name = anim_name

            # Unlink from armature so it's just stored in bpy.data.actions
            if temp_armature.animation_data:
                temp_armature.animation_data.action = old_action

            # Restore selection
            bpy.ops.object.select_all(action='DESELECT')
            for obj in old_selected:
                if obj:
                    obj.select_set(True)
            if old_active:
                bpy.context.view_layer.objects.active = old_active

            return action

        except Exception as e:
            print(f"Failed to import animation {anim_name}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def finish(self, context):
        """Clean up and run material import."""
        wm = context.window_manager
        wm.event_timer_remove(self._timer)

        # Import sequence animations if any were collected
        if self._import_animations and self._pending_animations:
            self._import_sequence_animations()

        # Track completed file
        completed = wm.lisr_completed_files.add()
        completed.name = self._json_filename
        completed.entity_count = self._total

        # Accumulate totals
        wm.lisr_total_objects += self._objects_imported

        self.report({'INFO'}, f"Imported {self._total} entities. Running material import...")
        bpy.ops.lis.mat_import(collection_name=self._collection_name)

        # Process next file in queue if any
        self._process_next_in_queue(context)

    def _process_next_in_queue(self, context):
        """Check if there are more files in the queue and start the next import."""
        wm = context.window_manager

        if wm.lisr_queue_total > 0:
            wm.lisr_queue_index += 1

            if wm.lisr_queue_index < wm.lisr_queue_total:
                # Get next file path
                next_path = wm.lisr_import_queue[wm.lisr_queue_index].path
                self.report({'INFO'}, f"Starting next file: {os.path.basename(next_path)}")

                # Use timer to delay next import - capture path in closure, not self
                def start_next(path=next_path):
                    bpy.ops.lis.map_import('INVOKE_DEFAULT', json_path=path)
                    return None  # Don't repeat

                bpy.app.timers.register(start_next, first_interval=0.1)
            else:
                # Queue complete
                wm.lisr_import_running = False
                wm.lisr_import_progress = 100.0
                wm.lisr_import_complete = True
                wm.lisr_current_file = ""

                # Count unique materials in imported collections
                material_count = len(bpy.data.materials)
                wm.lisr_total_materials = material_count

                wm.lisr_queue_total = 0
                wm.lisr_queue_index = 0
                wm.lisr_import_queue.clear()
                self.report({'INFO'}, "Import complete!")
        else:
            wm.lisr_import_running = False
            wm.lisr_import_progress = 100.0
            wm.lisr_import_complete = True

    def cancel(self, context):
        """Handle cancellation."""
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.lisr_import_running = False
        wm.lisr_queue_total = 0
        wm.lisr_queue_index = 0
        wm.lisr_import_queue.clear()
        wm.lisr_current_file = ""
        wm.lisr_completed_files.clear()
        self.report({'WARNING'}, "Import cancelled by user")


class MaterialImporter(bpy.types.Operator):
    """Optimized material importer with file indexing."""
    bl_idname = "lis.mat_import"
    bl_label = "Material Importer"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name: bpy.props.StringProperty(
        name="Collection Name",
        description="Name of the collection to process materials for",
        default=""
    )

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        mat_dir = prefs.base_directory

        # Use provided collection name or fall back to json_file basename
        collection_name = self.collection_name if self.collection_name else os.path.basename(prefs.json_file.replace('.json', ''))

        if collection_name not in bpy.data.collections:
            self.report({'WARNING'}, f"Collection '{collection_name}' not found for material import")
            return {'CANCELLED'}

        collection = bpy.data.collections[collection_name].objects

        # Build file index once - major optimization
        print("Building file index...")
        file_index = build_file_index(mat_dir, extensions=('.mat', '.tga', '.props.txt'))
        print(f"Indexed {len(file_index)} files")

        # Build material-to-slots mapping once for fast deduplication
        material_to_slots = defaultdict(list)
        for obj in bpy.context.scene.objects:
            for i, slot in enumerate(obj.material_slots):
                if slot.material:
                    material_to_slots[slot.material.name].append((obj, i))

        # Process materials
        materials_to_remove = []
        materials = list(bpy.data.materials)

        for material in materials:
            if material is None:
                continue

            if 'WorldGridMaterial' in material.name:
                materials_to_remove.append(material)
                continue

            # Clear existing nodes except Material Output
            if material.node_tree:
                for node in list(material.node_tree.nodes):
                    if node.name != "Material Output":
                        material.node_tree.nodes.remove(node)

            material.use_backface_culling = False
            mat_name = material.name
            split_matname = mat_name.split('.')

            # Handle duplicate materials (name.001, name.002, etc.)
            if len(split_matname) > 1:
                base_name = split_matname[0]
                replacement = bpy.data.materials.get(base_name)
                if replacement and replacement != material:
                    for obj, slot_idx in material_to_slots.get(mat_name, []):
                        obj.material_slots[slot_idx].material = replacement
                    materials_to_remove.append(material)
                continue

            mat_name = split_matname[0]

            # Find .mat file using index
            mat_filename = mat_name + '.mat'
            found_file = file_index.get(mat_filename)
            if not found_file:
                continue

            # Find and parse .props.txt file for material properties
            props_filename = mat_name + '.props.txt'
            found_props = file_index.get(props_filename)
            props_data = parse_props_file(found_props) if found_props else None

            # Apply material-level properties from props file
            if props_data:
                blend_mode = props_data.get('blend_mode', 0)
                if blend_mode == 0:  # Opaque
                    material.blend_method = 'OPAQUE'
                elif blend_mode == 1:  # Masked
                    material.blend_method = 'CLIP'
                    material.alpha_threshold = props_data.get('opacity_clip', 0.5)
                elif blend_mode == 2:  # Translucent
                    material.blend_method = 'BLEND'
                elif blend_mode == 3:  # Additive
                    material.blend_method = 'BLEND'

                material.use_backface_culling = not props_data.get('two_sided', False)

            # Parse material file for texture names
            diffuse_texturename = ''
            normal_texturename = ''
            spec_texturename = ''
            rough_texturename = ''

            with open(found_file) as mat_file:
                for line in mat_file:
                    if line.startswith(('Diffuse', 'Normal', 'SpecPower', 'Other[')):
                        splitline = line.split("=")
                        if len(splitline) > 1:
                            key = splitline[0]
                            value = splitline[1].strip()
                            if key == 'Diffuse':
                                diffuse_texturename = value
                            elif key == 'Normal':
                                normal_texturename = value
                            elif key == 'SpecPower':
                                spec_texturename = value
                            elif key.startswith('Other[') and value.endswith("R"):
                                rough_texturename = value

            if not rough_texturename and diffuse_texturename:
                rough_texturename = diffuse_texturename[:-1] + "R"

            if not diffuse_texturename and not normal_texturename:
                continue

            # Find texture files using index
            diffuse_texture_path = file_index.get(diffuse_texturename + '.tga') if diffuse_texturename else None
            normal_texture_path = file_index.get(normal_texturename + '.tga') if normal_texturename else None
            spec_texture_path = file_index.get(spec_texturename + '.tga') if spec_texturename else None
            rough_texture_path = file_index.get(rough_texturename + '.tga') if rough_texturename else None

            # Roughness priority logic using props.txt texture references
            texture_params = props_data.get('texture_params', {}) if props_data else {}
            use_normal_alpha_roughness = False

            # Priority 1: Dedicated RoughnessMap from props.txt (overrides .mat file)
            if 'RoughnessMap' in texture_params:
                props_rough_name = texture_params['RoughnessMap']
                props_rough_path = file_index.get(props_rough_name + '.tga')
                if props_rough_path:
                    rough_texture_path = props_rough_path
                    rough_texturename = props_rough_name

            # Priority 2: Normal map alpha (NormalMap+Roughness indicates roughness is in alpha)
            # This takes priority over .mat file fallback since .mat often has incorrect defaults
            if 'NormalMap+Roughness' in texture_params:
                # Get the normal map texture from props if available
                props_normal_name = texture_params.get('NormalMap+Roughness')
                props_normal_path = file_index.get(props_normal_name + '.tga') if props_normal_name else None
                check_normal_path = props_normal_path or normal_texture_path
                if check_normal_path and has_alpha_variation(check_normal_path):
                    use_normal_alpha_roughness = True
                    rough_texture_path = None  # Clear .mat roughness since we're using normal alpha
                    # Use the props normal map if it exists
                    if props_normal_path:
                        normal_texture_path = props_normal_path

            # Priority 3: .mat file fallback is already set above (rough_texture_path from .mat parsing)
            rough_texture_path = None

            # Setup shader nodes
            self._setup_material_nodes(
                material, diffuse_texture_path, normal_texture_path,
                spec_texture_path, rough_texture_path, props_data,
                use_normal_alpha_roughness
            )

        # Remove duplicate/invalid materials
        for mat in materials_to_remove:
            if mat:
                bpy.data.materials.remove(mat, do_unlink=True)

        # Remove mesh objects without materials (but keep lights, empties, etc.)
        objects_to_remove = []
        for obj in collection:
            if obj.type == 'MESH' and (len(obj.material_slots) == 0 or (len(obj.material_slots) > 0 and obj.material_slots[0].material is None)):
                objects_to_remove.append(obj)

        for obj in objects_to_remove:
            bpy.data.objects.remove(obj, do_unlink=True)

        print(f'Material import complete for {collection_name}')
        return {'FINISHED'}

    def _setup_material_nodes(self, material, diffuse_path, normal_path, spec_path, rough_path, props_data=None, use_normal_alpha_roughness=False):
        """Setup material shader nodes with textures."""
        if not material.node_tree:
            return

        is_decal = 'Decals' in material.name
        is_additive = props_data and props_data.get('blend_mode') == 3

        # Extract scalar parameters
        scalar_params = props_data.get('scalar_params', {}) if props_data else {}
        brightness_mult = scalar_params.get('BrightnessMult', 1.0)
        roughness_value = scalar_params.get('Roughness', scalar_params.get('RoughnessValue1', None))
        spec_value = scalar_params.get('Spec', scalar_params.get('Specular', None))

        # Node layout constants (X positions flow right to left)
        OUTPUT_X = 600
        SHADER_X = 300
        MULTIPLY_X = 100  # For brightness multiply node
        NORMAL_MAP_X = 0
        COMBINE_X = -200
        INVERT_X = -200
        SEPARATE_X = -400
        TEXTURE_X = -700

        # Y positions for different texture rows (texture nodes are ~250 tall)
        DIFFUSE_Y = 500
        NORMAL_Y = 150
        SPEC_Y = -250
        ROUGH_Y = -600

        if is_decal:
            shader_node = material.node_tree.nodes.new(type="ShaderNodeBsdfTransparent")
            shader_node.location = (SHADER_X, 0)
            if diffuse_path:
                diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                diffuse_texture.location = (TEXTURE_X, DIFFUSE_Y)
                diffuse_texture.image = bpy.data.images.load(diffuse_path)
                material.node_tree.links.new(shader_node.inputs["Color"], diffuse_texture.outputs["Color"])
        elif is_additive:
            # Additive materials use emission shader
            shader_node = material.node_tree.nodes.new(type="ShaderNodeEmission")
            shader_node.location = (SHADER_X, 0)

            emissive_power = scalar_params.get('EmissivePower', 1.0)
            shader_node.inputs["Strength"].default_value = emissive_power

            if diffuse_path:
                diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                diffuse_texture.location = (TEXTURE_X, DIFFUSE_Y)
                diffuse_texture.image = bpy.data.images.load(diffuse_path)
                material.node_tree.links.new(shader_node.inputs["Color"], diffuse_texture.outputs["Color"])
        else:
            shader_node = material.node_tree.nodes.new(type="ShaderNodeBsdfPrincipled")
            shader_node.location = (SHADER_X, 0)

            # Apply roughness from scalar params if no texture and not using normal alpha
            if roughness_value is not None and not rough_path and not use_normal_alpha_roughness:
                shader_node.inputs["Roughness"].default_value = roughness_value

            # Apply specular from scalar params if no texture
            if spec_value is not None and not spec_path:
                shader_node.inputs["Specular IOR Level"].default_value = spec_value

            if diffuse_path:
                diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                diffuse_texture.location = (TEXTURE_X, DIFFUSE_Y)
                diffuse_texture.image = bpy.data.images.load(diffuse_path)

                # Apply brightness multiplier if not 1.0
                if brightness_mult != 1.0:
                    multiply_node = material.node_tree.nodes.new(type="ShaderNodeMix")
                    multiply_node.data_type = 'RGBA'
                    multiply_node.blend_type = 'MULTIPLY'
                    multiply_node.location = (MULTIPLY_X, DIFFUSE_Y)
                    multiply_node.inputs["Factor"].default_value = 1.0
                    multiply_node.inputs["B"].default_value = (brightness_mult, brightness_mult, brightness_mult, 1.0)

                    material.node_tree.links.new(multiply_node.inputs["A"], diffuse_texture.outputs["Color"])
                    material.node_tree.links.new(shader_node.inputs["Base Color"], multiply_node.outputs["Result"])
                else:
                    material.node_tree.links.new(shader_node.inputs["Base Color"], diffuse_texture.outputs["Color"])

                material.node_tree.links.new(shader_node.inputs["Alpha"], diffuse_texture.outputs["Alpha"])

            if normal_path:
                normal_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                normal_texture.location = (TEXTURE_X, NORMAL_Y)
                normal_texture.image = bpy.data.images.load(normal_path)
                normal_texture.image.colorspace_settings.name = "Non-Color"

                normal_map_node = material.node_tree.nodes.new(type="ShaderNodeNormalMap")
                normal_map_node.location = (NORMAL_MAP_X, NORMAL_Y)
                normal_map_node.inputs["Strength"].default_value = 1.0

                separate_color = material.node_tree.nodes.new(type="ShaderNodeSeparateColor")
                separate_color.location = (SEPARATE_X, NORMAL_Y)
                invert_node = material.node_tree.nodes.new(type="ShaderNodeInvert")
                invert_node.location = (INVERT_X, NORMAL_Y - 150)
                combine_color = material.node_tree.nodes.new(type="ShaderNodeCombineColor")
                combine_color.location = (COMBINE_X, NORMAL_Y + 150)

                material.node_tree.links.new(separate_color.inputs["Color"], normal_texture.outputs["Color"])
                material.node_tree.links.new(invert_node.inputs["Color"], separate_color.outputs["Green"])
                material.node_tree.links.new(combine_color.inputs["Red"], separate_color.outputs["Red"])
                material.node_tree.links.new(combine_color.inputs["Green"], invert_node.outputs["Color"])
                material.node_tree.links.new(combine_color.inputs["Blue"], separate_color.outputs["Blue"])
                material.node_tree.links.new(normal_map_node.inputs["Color"], combine_color.outputs["Color"])
                material.node_tree.links.new(shader_node.inputs["Normal"], normal_map_node.outputs["Normal"])

                # Use normal texture's alpha channel for roughness if specified
                if use_normal_alpha_roughness:
                    material.node_tree.links.new(
                        shader_node.inputs["Roughness"],
                        normal_texture.outputs["Alpha"]
                    )

            if spec_path:
                spec_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                spec_texture.location = (TEXTURE_X, SPEC_Y)
                spec_texture.image = bpy.data.images.load(spec_path)
                spec_texture.image.colorspace_settings.name = "Non-Color"
                material.node_tree.links.new(shader_node.inputs[13], spec_texture.outputs["Color"])

            if rough_path and not use_normal_alpha_roughness:
                rough_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                rough_texture.location = (TEXTURE_X, ROUGH_Y)
                rough_texture.image = bpy.data.images.load(rough_path)
                rough_texture.image.colorspace_settings.name = "Non-Color"
                material.node_tree.links.new(shader_node.inputs[2], rough_texture.outputs["Color"])

        # Connect shader to output
        material_output = material.node_tree.nodes.get("Material Output")
        if material_output:
            material_output.location = (OUTPUT_X, 0)
            if is_additive:
                material.node_tree.links.new(shader_node.outputs["Emission"], material_output.inputs["Surface"])
            else:
                material.node_tree.links.new(shader_node.outputs["BSDF"], material_output.inputs["Surface"])


class ImportQueueItem(bpy.types.PropertyGroup):
    """Property group for import queue items."""
    path: bpy.props.StringProperty(name="Path")


classes = (
    MIAddonPreferences,
    CompletedFileItem,
    ImportQueueItem,
    VIEW3D_PT_map_importer_panel,
    BulkMapImporter,
    MapImporter,
    MaterialImporter
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Progress tracking
    bpy.types.WindowManager.lisr_import_progress = bpy.props.FloatProperty(
        name="Import Progress",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE'
    )
    bpy.types.WindowManager.lisr_import_running = bpy.props.BoolProperty(
        name="Import Running",
        default=False
    )
    bpy.types.WindowManager.lisr_import_queue = bpy.props.CollectionProperty(
        type=ImportQueueItem
    )
    bpy.types.WindowManager.lisr_queue_index = bpy.props.IntProperty(
        name="Queue Index",
        default=0
    )
    bpy.types.WindowManager.lisr_queue_total = bpy.props.IntProperty(
        name="Queue Total",
        default=0
    )

    # Import options
    bpy.types.WindowManager.lisr_import_meshes = bpy.props.BoolProperty(
        name="Import Meshes",
        description="Import static mesh components",
        default=True
    )
    bpy.types.WindowManager.lisr_import_lights = bpy.props.BoolProperty(
        name="Import Lights",
        description="Import light components",
        default=True
    )
    bpy.types.WindowManager.lisr_import_animations = bpy.props.BoolProperty(
        name="Import Animations",
        description="Import PSA animation files as Actions for skeletal meshes",
        default=False
    )
    bpy.types.WindowManager.lisr_import_sounds = bpy.props.BoolProperty(
        name="Import Sounds",
        description="Import 3D sound sources as Speakers",
        default=False
    )
    bpy.types.WindowManager.lisr_scale_factor = bpy.props.FloatProperty(
        name="Scale Factor",
        description="Scale factor for imported objects",
        default=1.0,
        min=0.01,
        max=100.0
    )

    # Status tracking
    bpy.types.WindowManager.lisr_current_file = bpy.props.StringProperty(
        name="Current File",
        default=""
    )
    bpy.types.WindowManager.lisr_entity_current = bpy.props.IntProperty(
        name="Current Entity",
        default=0
    )
    bpy.types.WindowManager.lisr_entity_total = bpy.props.IntProperty(
        name="Total Entities",
        default=0
    )
    bpy.types.WindowManager.lisr_import_complete = bpy.props.BoolProperty(
        name="Import Complete",
        default=False
    )
    bpy.types.WindowManager.lisr_total_objects = bpy.props.IntProperty(
        name="Total Objects",
        default=0
    )
    bpy.types.WindowManager.lisr_total_materials = bpy.props.IntProperty(
        name="Total Materials",
        default=0
    )
    bpy.types.WindowManager.lisr_total_maps = bpy.props.IntProperty(
        name="Total Maps",
        default=0
    )
    bpy.types.WindowManager.lisr_completed_files = bpy.props.CollectionProperty(
        type=CompletedFileItem
    )
    bpy.types.WindowManager.lisr_parent_collection = bpy.props.StringProperty(
        name="Parent Collection",
        description="Parent collection for current bulk import",
        default=""
    )


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    # Progress tracking
    del bpy.types.WindowManager.lisr_import_progress
    del bpy.types.WindowManager.lisr_import_running
    del bpy.types.WindowManager.lisr_import_queue
    del bpy.types.WindowManager.lisr_queue_index
    del bpy.types.WindowManager.lisr_queue_total

    # Import options
    del bpy.types.WindowManager.lisr_import_meshes
    del bpy.types.WindowManager.lisr_import_lights
    del bpy.types.WindowManager.lisr_import_animations
    del bpy.types.WindowManager.lisr_import_sounds
    del bpy.types.WindowManager.lisr_scale_factor

    # Status tracking
    del bpy.types.WindowManager.lisr_current_file
    del bpy.types.WindowManager.lisr_entity_current
    del bpy.types.WindowManager.lisr_entity_total
    del bpy.types.WindowManager.lisr_import_complete
    del bpy.types.WindowManager.lisr_total_objects
    del bpy.types.WindowManager.lisr_total_materials
    del bpy.types.WindowManager.lisr_total_maps
    del bpy.types.WindowManager.lisr_completed_files
    del bpy.types.WindowManager.lisr_parent_collection


if __name__ == "__main__":
    register()
