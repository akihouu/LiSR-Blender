bl_info = {
    "name": "LiS:R Map Importer",
    "author": "ZeoNyph",
    "version": (1,0,0),
    "blender": (4, 0, 0),
    "category": "Object",
    "description": r"Addon that imports .umap files from Life is Strange: Remastered into Blender",
}

import bpy
import json
import mathutils
from mathutils import Euler
import math
import os

class MISettings(bpy.types.PropertyGroup):

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

class VIEW3D_PT_map_importer_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "LiS:R Map Importer"
    bl_label = "Map Importer"

    def draw(self, context):
        scene = context.scene
        mytool = scene.my_tool

        self.layout.prop(mytool, "base_directory")
        self.layout.prop(mytool, "json_file")

        row = self.layout.row()
        row.operator(MapImporter.bl_idname, text="Import Map")
        self.layout.label(text="Blender will be unresponsive while the map is being imported.", icon="ERROR")

class MapImporter(bpy.types.Operator):
    bl_idname = "lis.map_import"
    bl_label = "Map Importer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        mytool = scene.my_tool
        

        # This is the base dir that contains all the unpacked assets - unpack using the latest ACL compatible build UE Viewer
        # Directory within must follow this path structure: LiS/Content/(your exported directories and files)
        base_dir = mytool.base_directory

        # This is the path to the JSON file that contains the map data - you can extract this from .umap files using FModel.exe
        map_json = [mytool.json_file]
        asset_sub_dir = ''

        # importer toggles
        import_static = True
        import_lights = False # enable to also import lights
        
        # Import types supported by the script
        static_mesh_types = [
            'StaticMeshComponent',
        #    'InstancedStaticMeshComponent' # buggy, positions wrong, seems to be used with splines as well
        ]
        light_types = [
            'SpotLightComponent',
            'AnimatedLightComponent',
            'PointLightComponent'
        ]

        def split_object_path(object_path):
            # For some reason ObjectPaths end with a period and a digit.
            # This is kind of a sucky way to split that out.
                
            path_parts = object_path.split(".")
            
            if len(path_parts) > 1:
                # Usually works, but will fail If the path contains multiple periods.
                return path_parts[0]
            
            # Nothing to do
            return object_path
            

        class StaticMesh:
            entity_name = ""
            import_path = ""
            pos = [0, 0, 0]
            rot = [0, 0, 0]
            scale = [1, 1, 1]
            
            # these are just properties to help with debugging
            no_entity = False
            no_file = False
            no_mesh = False
            no_path = False
            base_shape = False
            
            
            def __init__(self, json_entity, base_dir):
                self.entity_name = json_entity.get("Outer", 'Error')

                props = json_entity.get("Properties", None)
                if not props:
                    print('Invalid Entity: Lacking property')
                    self.no_entity = True
                    return None
                
                if not props.get("StaticMesh", None):
                    print('Invalid Property: does not contain a static mesh')
                    self.no_mesh = True
                    return None

                object_path = props.get("StaticMesh").get("ObjectPath", None)
                
                if not object_path or object_path == '':
                    print('Invalid StaticMesh: does not contain ObjectPath.')
                    self.no_path = True
                    return None

                if 'BasicShapes' in object_path:
                    # What is a BasicShape? Do we need these?
                    print('This is a BasicShape - skipping for now')
                    self.base_shape = True
                    return None
                
                objpath = split_object_path(object_path)
                self.import_path = base_dir + asset_sub_dir + objpath + ".gltf"
                print('Mesh Path', self.import_path)
                self.no_file = not os.path.exists(self.import_path)

                if props.get("RelativeLocation", False):
                    pos = props.get("RelativeLocation")
                    self.pos = [pos.get("X")/100,pos.get("Y")/-100,pos.get("Z")/100]
                
                if props.get("RelativeRotation", False):
                    rot = props.get("RelativeRotation")
                    self.rot = [rot.get("Roll"),rot.get("Pitch")*-1,rot.get("Yaw")*-1]
                
                if props.get("RelativeScale3D", False):
                    scale = props.get("RelativeScale3D")
                    self.scale = [scale.get("X", 1),scale.get("Y", 1),scale.get("Z", 1)]
                
                return None
            
            @property
            def invalid(self):
                return self.no_path or self.no_file or self.no_entity or self.base_shape or self.no_mesh
                

            def import_staticmesh(self, collection):
                if self.invalid:
                    print('Refusing to import due to failed checks.')
                    return False
                # Import the file and apply transforms
                bpy.ops.import_scene.gltf(filepath=self.import_path)
                imported_obj = bpy.context.object
                
                imported_obj.name = self.entity_name
                imported_obj.scale = (self.scale[0], self.scale[1], self.scale[2])
                imported_obj.location = (self.pos[0], self.pos[1], self.pos[2])
                imported_obj.rotation_mode = 'XYZ'
                imported_obj.rotation_euler = Euler((math.radians(self.rot[0]), math.radians(self.rot[1]), math.radians(self.rot[2])), 'XYZ')
                collection.objects.link(imported_obj)
                bpy.context.scene.collection.objects.unlink(imported_obj)

                print('StaticMesh imported:', self.entity_name)
                return imported_obj


        class GameLight:
            entity_name = ""
            type = ""

            pos = [0, 0, 0]
            rot = [0, 0, 0]
            scale = [1, 1, 1]

            energy = 1000

            no_entity = False

            def __init__(self, json_entity):
                self.entity_name = json_entity.get("Outer", 'Error')
                self.type = json_entity.get("SpotLightComponent", "SpotLightComponent")

                props = json_entity.get("Properties", None)
                if not props:
                    print('Invalid Entity: Lacking property')
                    self.no_entity = True
                    return None
                
                if props.get("RelativeLocation", False):
                    pos = props.get("RelativeLocation")
                    self.pos = [pos.get("X")/100,pos.get("Y")/-100,pos.get("Z")/100]
                
                if props.get("RelativeRotation", False):
                    rot = props.get("RelativeRotation")
                    self.rot = [rot.get("Roll"),rot.get("Pitch")*-1,rot.get("Yaw")*-1]
                
                if props.get("RelativeScale3D", False):
                    scale = props.get("RelativeScale3D")
                    self.scale = [scale.get("X", 1),scale.get("Y", 1),scale.get("Z", 1)]

                #TODO: expand this method with more properties for the specific light types
                # Problem: I don't know how values for UE lights map to Blender's light types.
            
            def import_light(self, collection):
                if self.no_entity:
                    print('Refusing to import due to failed checks.')
                    return False
                print('importing light')
                if self.type == 'SpotLightComponent':
                    light_data = bpy.data.lights.new(name=self.entity_name, type='SPOT')
                if self.type == 'PointLightComponent':
                    light_data = bpy.data.lights.new(name=self.entity_name, type='POINT')
                
                light_obj = bpy.data.objects.new(name=self.entity_name, object_data=light_data)
                light_obj.scale = (self.scale[0], self.scale[1], self.scale[2])
                light_obj.location = (self.pos[0], self.pos[1], self.pos[2])
                light_obj.rotation_mode = 'XYZ'
                light_obj.rotation_euler = Euler((math.radians(self.rot[0]), math.radians(self.rot[1]), math.radians(self.rot[2])), 'XYZ')
                collection.objects.link(light_obj)
                bpy.context.scene.collection.objects.link(light_obj)


        # SCRIPT STARTS DOING STUFF HERE
        for map in map_json:
            print('Processing file', map)

            if not os.path.exists(map):
                print('File not found, skipping.', map)
                continue

            json_filename = os.path.basename(map)
            import_collection = bpy.data.collections.new(json_filename)
            
            bpy.context.scene.collection.children.link(import_collection)
            with open(map) as file: 
                json_object = json.load(file)
                print("-------------============================-------------")

                # Handle the different entity types
                for entity in json_object:
                    if not entity.get('Type', None):
                        continue

                    if import_lights and entity.get('Type') in light_types:
                        print(entity)
                        light = GameLight(entity)
                        light.import_light(import_collection)

                    if import_static and entity.get('Type') in static_mesh_types:
                        static_mesh = StaticMesh(entity, base_dir)
                        # TODO: optimize by instancing certain meshes
                        static_mesh.import_staticmesh(import_collection)
                        continue
        print('Done.')
        bpy.ops.lis.mat_import()
        return {'FINISHED'}
    
class MaterialImporter(bpy.types.Operator):
    bl_idname= "lis.mat_import"
    bl_label="Material Importer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        mytool = scene.my_tool
        collection = bpy.data.collections[os.path.basename(mytool.json_file)].objects

        def dedup_materials(material_name_to_replace, replacement_material_name):
            # replaces and deletes any duplicate materials in the scene

            materials = bpy.data.materials

            # Get the material to use as replacement
            replacement_material = materials.get(replacement_material_name)

            if replacement_material is None:
                print(f"Error: Material '{replacement_material_name}' not found.")
                return None
            
            # Iterate over all objects in the scene
            for obj in bpy.context.scene.objects:
                # Check if the object has a material slot with the material to replace
                for i, slot in enumerate(obj.material_slots):
                    if slot.material is not None and slot.material.name == material_name_to_replace:
                        # Replace the material with the replacement material
                        slot.material = replacement_material
                        print(f"Replaced material in object '{obj.name}', slot {i}.")


        def search_directory(root_dir, file_name):
            # utility function to search for a file in a directory
            for subdir, dirs, files in os.walk(root_dir):
                for file in files:
                    if file == file_name:
                        file_path = os.path.join(subdir, file)
                        return file_path
            return None

        mat_dir = mytool.base_directory # add dir here

        # Get all materials in the scene
        materials = bpy.data.materials

        # Iterate over all materials and print their names
        for material in materials:
            if 'WorldGridMaterial' in material.name:
                #TODO: Also remove objects using this material
                bpy.data.materials.remove(material, do_unlink=True)
                continue
            for node in material.node_tree.nodes:
                if node != material.node_tree.nodes["Material Output"]:
                    material.node_tree.nodes.remove(node)
            
            # Disable Backface Culling - this will make the material double sided
            material.use_backface_culling = False
            mat_name = material.name
            split_matname = mat_name.split('.')
            
            # dedup material
            if len(split_matname) > 1:
                dedup_materials(material.name, split_matname[0])
                bpy.data.materials.remove(material, do_unlink=True)
                continue

            mat_name = split_matname[0]

            # Find the .mat file so we can pull the texture names out of it
            found_file = search_directory(mat_dir, mat_name + '.mat')
            if not found_file:
                print('No material found.')
                continue
            
            # TODO: this is ugly, clean it up
            diffuse_texturename = ''
            normal_texturename = ''
            spec_texturename = ''
            rough_texturename = ''
            with open(found_file) as mat_file:
                lines = mat_file.readlines()
                
                for line in lines:
                    if line.startswith('Diffuse') or line.startswith('Normal') or line.startswith('SpecPower') or line.startswith('Other[0]'):
                        splitline = line.split("=")
                        if len(splitline) > 1:
                            if splitline[0] == 'Diffuse':
                                diffuse_texturename = splitline[1].strip()
                            if splitline[0] == 'Normal':
                                normal_texturename = splitline[1].strip()
                            if splitline[0] == 'SpecPower':
                                spec_texturename = splitline[1].strip()
                            if splitline[0].startswith('Other[') and splitline[1].endswith("R"):
                                rough_texturename = splitline[1].strip()
                            else: rough_texturename = diffuse_texturename[:len(diffuse_texturename)-1] + "R"


            if not diffuse_texturename and not normal_texturename:
                print('We have no textures. Skipping.')
                continue

            diffuse_texture_path = None
            normal_texture_path = None
            spec_texture_path = None
            rough_texture_path = None
            if diffuse_texturename != '':
                diffuse_texture_path = search_directory(mat_dir, diffuse_texturename + '.tga')
            if normal_texturename != '':
                normal_texture_path = search_directory(mat_dir, normal_texturename + '.tga')
            if spec_texturename != '':
                spec_texture_path = search_directory(mat_dir, spec_texturename + '.tga')
            if rough_texturename != '':
                rough_texture_path = search_directory(mat_dir, rough_texturename  + '.tga')
                print(rough_texture_path)

            # This convoluted mess is to set up the textures and connect all the material nodes
            
            # Create a new Principled BSDF shader node for the material
            # TODO: check if this node already exists
            
            shader_node = None
            
            if 'Decals' in material.name:
                shader_node = material.node_tree.nodes.new(type="ShaderNodeBsdfTransparent")
                
                if diffuse_texture_path is not None:
                    print("DIFF: " + diffuse_texture_path)
                    # Set the diffuse map for the material
                    diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                    diffuse_texture.image = bpy.data.images.load(diffuse_texture_path)
                    material.node_tree.links.new(shader_node.inputs["Color"], diffuse_texture.outputs["Color"])
            else:   
                shader_node = material.node_tree.nodes.new(type="ShaderNodeBsdfPrincipled")

                if diffuse_texture_path is not None:
                    print("DIFF: " + diffuse_texture_path)
                    # Set the diffuse map for the material
                    diffuse_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                    diffuse_texture.image = bpy.data.images.load(diffuse_texture_path)
                    material.node_tree.links.new(shader_node.inputs["Base Color"], diffuse_texture.outputs["Color"])
                    material.node_tree.links.new(shader_node.inputs["Alpha"], diffuse_texture.outputs["Alpha"])

                if normal_texture_path is not None:
                    # Set the normal map for the material
                    normal_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                    normal_texture.image = bpy.data.images.load(normal_texture_path)
                    normal_texture.image.colorspace_settings.name = "Non-Color"
                    normal_map_node = material.node_tree.nodes.new(type="ShaderNodeNormalMap")
                    normal_map_node.inputs["Strength"].default_value = 1.0
                    material.node_tree.links.new(shader_node.inputs["Normal"], normal_map_node.outputs["Normal"])
                    
                    ##
                    # Create nodes to invert the green channel
                    
                    seperate_color = material.node_tree.nodes.new(type="ShaderNodeSeparateColor")
                    invert_node = material.node_tree.nodes.new(type="ShaderNodeInvert")
                    combine_color = material.node_tree.nodes.new(type="ShaderNodeCombineColor")
                    
                    # link remaining separate color inputs to combine color
                    material.node_tree.links.new(combine_color.inputs["Red"], seperate_color.outputs["Red"])
                    material.node_tree.links.new(combine_color.inputs["Blue"], seperate_color.outputs["Blue"])
                    
                    # link sep to invert
                    material.node_tree.links.new(invert_node.inputs["Color"], seperate_color.outputs["Green"])
                    # link invert to comb
                    material.node_tree.links.new(combine_color.inputs["Green"], invert_node.outputs["Color"])
                    
                    # wire up the rest
                    material.node_tree.links.new(seperate_color.inputs["Color"], normal_texture.outputs["Color"])   
                    material.node_tree.links.new(normal_map_node.inputs["Color"], combine_color.outputs["Color"])

                if spec_texture_path is not None:
                    print("SPEC:" + spec_texture_path)
                    spec_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                    spec_texture.image = bpy.data.images.load(spec_texture_path)
                    spec_texture.image.colorspace_settings.name = "Non-Color"
                    material.node_tree.links.new(shader_node.inputs[13], spec_texture.outputs["Color"])

                if rough_texture_path is not None:
                    print("ROUGH: " + rough_texture_path)
                    rough_texture = material.node_tree.nodes.new(type="ShaderNodeTexImage")
                    rough_texture.image = bpy.data.images.load(rough_texture_path)
                    rough_texture.image.colorspace_settings.name = "Non-Color"
                    material.node_tree.links.new(shader_node.inputs[2], rough_texture.outputs["Color"])


            # Get the material output and wire it up to the shader node
            material_output = material.node_tree.nodes.get("Material Output")

            if material_output is None:
                print(f"Error: Material output node not found in material '{material.name}'.")

            material.node_tree.links.new(shader_node.outputs["BSDF"], material_output.inputs["Surface"])

        print('Done')


        # Remove objects without materials
        # Iterate over all objects in the scene
        for obj in collection:
            # Check if the object has a material slot in the first position
            if len(obj.material_slots) > 0:
                # Check if the material slot in the first position is empty
                if obj.material_slots[0].material is None:
                    # Object has no material assigned to the first slot
                    print(f"Object '{obj.name}' has no material assigned to the first slot.")
                    bpy.data.objects.remove(obj, do_unlink=True)
            else:
                # Object has no material slots
                print(f"Object '{obj.name}' has no material slots.")
        return {'FINISHED'}


classes = (MISettings, VIEW3D_PT_map_importer_panel, MapImporter, MaterialImporter)

def register():
    bpy.types.WindowManager.progress = bpy.props.FloatProperty()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.my_tool = bpy.props.PointerProperty(type=MISettings)
def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.my_tool
if __name__ == "__main__":
    register()