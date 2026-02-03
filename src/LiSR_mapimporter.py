bl_info = {
    "name": "LiS:R Map Importer",
    "author": "ZeoNyph",
    "version": (1,2,0),
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


def build_file_index(root_dir, extensions=('.mat', '.tga')):
    """Build filename -> path mapping once for fast lookups."""
    index = {}
    for subdir, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(extensions):
                index[file] = os.path.join(subdir, file)
    return index


def split_object_path(object_path):
    """Split ObjectPath, removing trailing period and digit."""
    path_parts = object_path.split(".")
    if len(path_parts) > 1:
        return path_parts[0]
    return object_path


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


class VIEW3D_PT_map_importer_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "LiS:R Map Importer"
    bl_label = "Map Importer"

    def draw(self, context):
        prefs = context.preferences.addons[__name__].preferences
        wm = context.window_manager

        self.layout.prop(prefs, "base_directory")
        self.layout.prop(prefs, "json_file")

        row = self.layout.row()
        row.operator(MapImporter.bl_idname, text="Import Map")
        row.operator(BulkMapImporter.bl_idname, text="Bulk Import")

        if wm.lisr_import_running:
            self.layout.prop(wm, "lisr_import_progress", text="Progress")
            if wm.lisr_queue_total > 1:
                self.layout.label(text=f"File {wm.lisr_queue_index + 1} of {wm.lisr_queue_total}")
        else:
            self.layout.label(text="Import will run in background with progress updates.", icon="INFO")


class StaticMesh:
    """Represents a static mesh entity to be imported."""

    def __init__(self, json_entity, base_dir, asset_sub_dir=''):
        self.entity_name = json_entity.get("Outer", 'Error')
        self.import_path = ""
        self.pos = [0, 0, 0]
        self.rot = [0, 0, 0]
        self.scale = [1, 1, 1]
        self.invalid = False
        self.skip_reason = ""

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

        if props.get("RelativeLocation", False):
            pos = props.get("RelativeLocation")
            self.pos = [pos.get("X")/100, pos.get("Y")/-100, pos.get("Z")/100]

        if props.get("RelativeRotation", False):
            rot = props.get("RelativeRotation")
            self.rot = [rot.get("Roll"), rot.get("Pitch")*-1, rot.get("Yaw")*-1]

        if props.get("RelativeScale3D", False):
            scale = props.get("RelativeScale3D")
            self.scale = [scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1)]


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


class BulkMapImporter(bpy.types.Operator):
    """Open file browser to select multiple .umap JSON files for bulk import."""
    bl_idname = "lis.bulk_map_import"
    bl_label = "Bulk Import Maps"
    bl_options = {'REGISTER', 'UNDO'}

    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
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

        self.report({'INFO'}, f"Starting bulk import of {len(file_paths)} files...")

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
    _import_lights = False
    _static_mesh_types = ['StaticMeshComponent']
    _light_types = ['SpotLightComponent', 'AnimatedLightComponent', 'PointLightComponent']
    _batch_size = 5

    def invoke(self, context, event):
        prefs = context.preferences.addons[__name__].preferences
        wm = context.window_manager

        self._base_dir = prefs.base_directory

        # Use provided json_path or fall back to preferences
        map_json_path = self.json_path if self.json_path else prefs.json_file

        if not os.path.exists(map_json_path):
            self.report({'ERROR'}, f"JSON file not found: {map_json_path}")
            return {'CANCELLED'}

        # Load and parse JSON
        with open(map_json_path) as file:
            json_object = json.load(file)

        # Filter to only importable entities
        self._entities = []
        for entity in json_object:
            entity_type = entity.get('Type', None)
            if not entity_type:
                continue
            if self._import_static and entity_type in self._static_mesh_types:
                self._entities.append(('mesh', entity))
            elif self._import_lights and entity_type in self._light_types:
                self._entities.append(('light', entity))

        self._total = len(self._entities)
        self._index = 0
        self._mesh_cache = {}

        if self._total == 0:
            self.report({'WARNING'}, "No importable entities found in JSON")
            # Check if there are more files in queue
            self._process_next_in_queue(context)
            return {'CANCELLED'}

        # Create collection
        json_filename = os.path.basename(map_json_path)
        self._collection_name = json_filename
        self._collection = bpy.data.collections.new(json_filename)
        context.scene.collection.children.link(self._collection)

        # Setup progress
        wm.lisr_import_running = True
        wm.lisr_import_progress = 0.0

        # Start timer
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)

        self.report({'INFO'}, f"Starting import of {self._total} entities from {json_filename}...")
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
                    self._import_mesh_entity(entity)
                elif entity_type == 'light':
                    light = GameLight(entity)
                    light.import_light(self._collection)

            self._index = end_index

            # Update progress
            progress = self._index / self._total
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
        static_mesh = StaticMesh(entity, self._base_dir)

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

    def finish(self, context):
        """Clean up and run material import."""
        wm = context.window_manager
        wm.event_timer_remove(self._timer)

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
                wm.lisr_queue_total = 0
                wm.lisr_queue_index = 0
                wm.lisr_import_queue.clear()
                self.report({'INFO'}, "Bulk import complete!")
        else:
            wm.lisr_import_running = False
            wm.lisr_import_progress = 100.0

    def cancel(self, context):
        """Handle cancellation."""
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.lisr_import_running = False
        wm.lisr_queue_total = 0
        wm.lisr_queue_index = 0
        wm.lisr_import_queue.clear()
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
        collection_name = self.collection_name if self.collection_name else os.path.basename(prefs.json_file)

        if collection_name not in bpy.data.collections:
            self.report({'WARNING'}, f"Collection '{collection_name}' not found for material import")
            return {'CANCELLED'}

        collection = bpy.data.collections[collection_name].objects

        # Build file index once - major optimization
        print("Building file index...")
        file_index = build_file_index(mat_dir, extensions=('.mat', '.tga'))
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

            # Setup shader nodes
            self._setup_material_nodes(
                material, diffuse_texture_path, normal_texture_path,
                spec_texture_path, rough_texture_path
            )

        # Remove duplicate/invalid materials
        for mat in materials_to_remove:
            if mat:
                bpy.data.materials.remove(mat, do_unlink=True)

        # Remove objects without materials
        objects_to_remove = []
        for obj in collection:
            if len(obj.material_slots) == 0 or (len(obj.material_slots) > 0 and obj.material_slots[0].material is None):
                objects_to_remove.append(obj)

        for obj in objects_to_remove:
            bpy.data.objects.remove(obj, do_unlink=True)

        print(f'Material import complete for {collection_name}')
        return {'FINISHED'}

    def _setup_material_nodes(self, material, diffuse_path, normal_path, spec_path, rough_path):
        """Setup material shader nodes with textures."""
        if not material.node_tree:
            return

        is_decal = 'Decals' in material.name

        # Node layout constants (X positions flow right to left)
        OUTPUT_X = 600
        SHADER_X = 300
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
        else:
            shader_node = material.node_tree.nodes.new(type="ShaderNodeBsdfPrincipled")
            shader_node.location = (SHADER_X, 0)

            if diffuse_path:
                diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                diffuse_texture.location = (TEXTURE_X, DIFFUSE_Y)
                diffuse_texture.image = bpy.data.images.load(diffuse_path)
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

            if spec_path:
                spec_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                spec_texture.location = (TEXTURE_X, SPEC_Y)
                spec_texture.image = bpy.data.images.load(spec_path)
                spec_texture.image.colorspace_settings.name = "Non-Color"
                material.node_tree.links.new(shader_node.inputs[13], spec_texture.outputs["Color"])

            if rough_path:
                rough_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                rough_texture.location = (TEXTURE_X, ROUGH_Y)
                rough_texture.image = bpy.data.images.load(rough_path)
                rough_texture.image.colorspace_settings.name = "Non-Color"
                material.node_tree.links.new(shader_node.inputs[2], rough_texture.outputs["Color"])

        # Connect shader to output
        material_output = material.node_tree.nodes.get("Material Output")
        if material_output:
            material_output.location = (OUTPUT_X, 0)
            material.node_tree.links.new(shader_node.outputs["BSDF"], material_output.inputs["Surface"])


class ImportQueueItem(bpy.types.PropertyGroup):
    """Property group for import queue items."""
    path: bpy.props.StringProperty(name="Path")


classes = (
    MIAddonPreferences,
    VIEW3D_PT_map_importer_panel,
    ImportQueueItem,
    BulkMapImporter,
    MapImporter,
    MaterialImporter
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

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


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.WindowManager.lisr_import_progress
    del bpy.types.WindowManager.lisr_import_running
    del bpy.types.WindowManager.lisr_import_queue
    del bpy.types.WindowManager.lisr_queue_index
    del bpy.types.WindowManager.lisr_queue_total


if __name__ == "__main__":
    register()
