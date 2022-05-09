"""
CEASIOMpy: Conceptual Aircraft Design Software

Developed by CFS ENGINEERING, 1015 Lausanne, Switzerland

Use .brep files parts of an airplane to generate a fused airplane in GMSH with
the OCC kernel. Then Spherical farfield is created around the airplane and the
resulting domain is meshed using gmsh

Python version: >=3.7

| Author: Tony Govoni
| Creation: 2022-03-22

TODO:

    - Make add to ModelPart the mesh size and mesh color
    - Add the possibility to change the symmetry plane orientation

"""


# =================================================================================================
#   IMPORTS
# =================================================================================================

from pathlib import Path

import gmsh
import numpy as np
from ceasiompy.CPACS2GMSH.func.advancemeshing import (
    refine_wing_section,
    set_farfield_mesh,
    set_fuselage_mesh,
)
from ceasiompy.CPACS2GMSH.func.wingclassification import classify_wing
from ceasiompy.utils.ceasiomlogger import get_logger
from ceasiompy.utils.ceasiompyutils import get_part_type

log = get_logger()

MESH_COLORS = {
    "farfield": (255, 200, 0),
    "symmetry": (138, 43, 226),
    "wing": (0, 200, 200),
    "fuselage": (255, 215, 0),
    "pylon": (255, 0, 0),
}

# =================================================================================================
#   CLASSES
# =================================================================================================


class ModelPart:
    """
    A class to represent part of the aircraft or other part of the gmsh model
    in order to classify its entities and dimension tags
    ...

    Attributes
    ----------
    uid : str
        name of the part which correspond to its .brep file name for aircraft parts
        or a simple name describing the part function in the model

    """

    def __init__(self, uid):

        self.uid = uid
        self.part_type = ""

        # dimtag
        self.points = []
        self.lines = []
        self.surfaces = []
        self.volume = []
        # tag only
        self.points_tags = []
        self.lines_tags = []
        self.surfaces_tags = []
        self.volume_tag = []
        # children
        self.children_dimtag = set()

    def associate_child_to_parent(self, child_dimtag):
        """
        Function to associate a child to a parent.
        all the entities belonging to a child (volume generated by the fragment operation)
        are associated to their parent part (volume before the fragment operation)

        Args:
        ----------
        child_dimtag : tuple (dim,tag)
            dimtag of the child volume

        """

        child_volume = [child_dimtag]

        child_surfaces, child_lines, child_points = get_entities_from_volume(child_volume)

        # first get the dimtags
        child_volume_tag = [dimtag[1] for dimtag in child_volume]
        child_surfaces_tags = [dimtag[1] for dimtag in child_surfaces]
        child_lines_tags = [dimtag[1] for dimtag in child_lines]
        child_points_tags = [dimtag[1] for dimtag in child_points]

        # store in parent parts for latter use
        self.points.extend(child_points)
        self.lines.extend(child_lines)
        self.surfaces.extend(child_surfaces)
        self.volume.extend(child_volume)

        self.points_tags.extend(child_points_tags)
        self.lines_tags.extend(child_lines_tags)
        self.surfaces_tags.extend(child_surfaces_tags)
        self.volume_tag.extend(child_volume_tag)

    def clean_inside_entities(self, final_domain):
        """
        Function to clean inside entities of the part.
        Inside entities are entities that are not part of the final domain.

        Args:
        ----------
        final_domain : ModelPart
            final_domain part
        """
        # detect only shared entities with the final domain

        self.surfaces = list(set(self.surfaces).intersection(set(final_domain.surfaces)))
        self.lines = list(set(self.lines).intersection(set(final_domain.lines)))
        self.points = list(set(self.points).intersection(set(final_domain.points)))

        self.surfaces_tags = list(
            set(self.surfaces_tags).intersection(set(final_domain.surfaces_tags))
        )
        self.lines_tags = list(set(self.lines_tags).intersection(set(final_domain.lines_tags)))
        self.points_tags = list(set(self.points_tags).intersection(set(final_domain.points_tags)))


# =================================================================================================
#   FUNCTIONS
# =================================================================================================


def get_entities_from_volume(volume_dimtag):
    """
    Function to get the entities belonging to a volume.
    Surfaces and lines are found with the gmsh.model.getBoundary() function.
    Points are found with the gmsh.model.getEntities() function using recursive set to True.
    This choice seems the most efficient and robust at the state of development of Gmsh

    Args:
    ----------
    volume_dimtag : list
        a list containing the dimtag of the volume [(dim,tag)]
        which is a standard input format for other gmsh function
    ...

    Returns:
    ----------
    surfaces_dimtags : list(tuple)
        a list of tuples containing the dimtag of the surfaces
    lines_dimtags : list(tuple)
        a list of tuples containing the dimtag of the lines
    points_dimtags : list(tuple)
        a list of tuples containing the dimtag of the points
    """

    surfaces_dimtags = gmsh.model.getBoundary(
        volume_dimtag, combined=True, oriented=False, recursive=False
    )

    lines_dimtags = list(
        set().union(
            *[
                gmsh.model.getBoundary([surface], combined=True, oriented=False, recursive=False)
                for surface in surfaces_dimtags
            ]
        )
    )
    lines_dimtags.sort()
    points_dimtags = list(
        set().union(
            *[
                gmsh.model.getBoundary([surface], combined=True, oriented=False, recursive=True)
                for surface in surfaces_dimtags
            ]
        )
    )
    points_dimtags.sort()

    return surfaces_dimtags, lines_dimtags, points_dimtags


def process_gmsh_log(gmsh_log):
    """
    Function to process the gmsh log file.
    It is used to retrieve the mesh quality
    ...

    Args:
    ----------
    gmsh_log : list(str)
        list of gmsh log events
    """

    # find log about mesh quality
    quality_log = [log for log in gmsh_log if "< quality <" in log]

    # get only the last ten quality log
    final_quality_log = quality_log[-10:]

    # print log with ceasiompy logger
    log.info("Final mesh quality :")
    for log_line in final_quality_log:
        log.info(log_line)


def generate_gmsh(
    cpacs_path,
    brep_dir_path,
    results_dir,
    open_gmsh=False,
    farfield_factor=5,
    symmetry=False,
    mesh_size_farfield=12,
    mesh_size_fuselage=0.2,
    mesh_size_wings=0.2,
    refine_factor=4,
):
    """
    Function to generate a mesh from brep files forming an airplane
    Function 'generate_gmsh' is a subfunction of CPACS2GMSH which return a
    mesh file.
    The airplane is fused with the different brep files : fuselage, wings and
    other parts are identified anf fused together, then a farfield is generated
    and the airplane is substracted to him to generate the final fluid domain
    marker of each airplane part and farfield surfaces is reported in the mesh
    file.
    Args:
    ----------
    cpacs_path : str
        path to the cpacs file
    brep_dir_path : (Path)
        Path to the directory containing the brep files
    results_dir : (path)
        Path to the directory containing the result (mesh) files
    open_gmsh : bool
        Open gmsh GUI after the mesh generation if set to true
    farfield_factor = float
        Factor to enlarge the farfield : factor times the largest dimension(x,y,z)
        of the aircraft
    symmetry : bool
        If set to true, the mesh will be generated with symmetry wrt the x,z plane
    mesh_size_farfield : float
        Size of the farfield mesh
    mesh_size_fuselage : float
        Size of the fuselage mesh
    mesh_size_wings : float
        Size of the wing mesh
    advance_mesh : bool
        If set to true, the mesh will be generated with advanced meshing options
    refine_factor : int
        refine factor for the mesh le and te edge

    """

    brep_files = list(brep_dir_path.glob("*.brep"))
    brep_files.sort()

    gmsh.initialize()
    # Stop gmsh output log in the terminal
    gmsh.option.setNumber("General.Terminal", 0)
    # Log complexity
    gmsh.option.setNumber("General.Verbosity", 5)

    # import each aircraft original parts / parent parts
    aircraft_parts = []
    parts_parent_dimtag = []
    log.info(f"Importing files from {brep_dir_path}")
    for brep_file in brep_files:

        # Import the part and create the aircraft part object
        part_entities = gmsh.model.occ.importShapes(str(brep_file), highestDimOnly=False)
        gmsh.model.occ.synchronize()

        # Create the aircraft part object
        part_obj = ModelPart(uid=brep_file.stem)
        part_obj.part_type = get_part_type(cpacs_path, part_obj.uid)

        # Add to the list of aircraft parts
        aircraft_parts.append(part_obj)
        parts_parent_dimtag.append(part_entities[0])

        log.info(f"Part : {part_obj.uid} imported")

    gmsh.model.occ.synchronize()

    # create external domain for the farfield
    model_bb = gmsh.model.getBoundingBox(-1, -1)
    model_dimensions = [
        abs(model_bb[0] - model_bb[3]),
        abs(model_bb[1] - model_bb[4]),
        abs(model_bb[2] - model_bb[5]),
    ]
    model_center = [
        model_bb[0] + model_dimensions[0] / 2,
        model_bb[1] + model_dimensions[1] / 2,
        model_bb[2] + model_dimensions[2] / 2,
    ]

    domain_length = farfield_factor * max(model_dimensions)
    farfield = gmsh.model.occ.addSphere(*model_center, domain_length)
    gmsh.model.occ.synchronize()

    ext_domain = [(3, farfield)]

    if symmetry:
        log.info("Preparing: symmetry operation")
        sym_plane = gmsh.model.occ.addDisk(*model_center, domain_length, domain_length)
        sym_vector = [0, 1, 0]
        plane_vector = [0, 0, 1]
        if sym_vector != plane_vector:
            rotation_axis = np.cross(sym_vector, plane_vector)
            gmsh.model.occ.rotate([(2, sym_plane)], *model_center, *rotation_axis, np.pi / 2)
            sym_box = gmsh.model.occ.extrude(
                [(2, sym_plane)], *(np.multiply(sym_vector, -domain_length * 1.1))
            )
        parts_parent_dimtag.append(sym_box[1])

    # Generate fragment between the aircraft and the farfield

    log.info("Start fragment operation")

    _, children_dimtag = gmsh.model.occ.fragment(ext_domain, parts_parent_dimtag)
    gmsh.model.occ.synchronize()

    log.info("Fragment operation finished")

    # fragment produce fragments_dimtag and children_dimtag

    # fragments_dimtag is a list of tuples (dimtag, tag) of all the volumes in the model
    # the first fragment is the entire domain, each other fragment are subvolume of the domain

    # children_dimtag is a list list of tuples (dimtag, tag)
    # the first list is associated to the entire domain as for fragments_dimtag, we don't need it
    # so for the following we work with children_dimtag[1:]

    # The rest of children_dimtag are list of tuples (dimtag, tag) that represent volumes in the
    # model children_dimtag is "sorted" according to the order of importation of the parent parts.
    # for example : if the first part imported was "fuselage1" then the first children_dimtag
    # is a list of all the "child" volumes in the model that are from the "parent" "fuselage1"
    # we can then associate each entities in the model to their parent origin

    # When two parents part ex. a fuselage and a wing intersect each other
    # two children are generated for both parts, thus if a child is shared by
    # two parent parts (or more), then this child is a volume given
    # by the intersection of the two parent parts, we don't need them and some
    # of its surfaces, lines and point in the final models

    # Thus we need to find those unwanted child and their entities that don't belong
    # to the final model, and remove them

    # afterward the entities of each child will be associated with their parent part names
    # then we can delete all the child in the model, and only keep the final domain
    # Removing a child will not delete its entities shared by the final domain, this means that
    # at the end we will only have one volume with all the surfaces,lines,points assigned
    # to the original parent parts imported at the begging of the function

    # If symmetry is applied the last children_dimtag is all the volume in the symmetry cylinder
    # thus the we can easily remove them and only keep the volumes of half domain

    unwanted_children = []
    if symmetry:
        # take the unwanted children from symmetry
        unwanted_children = children_dimtag[-1]

        # remove them from the model
        gmsh.model.occ.remove(unwanted_children, recursive=True)
        gmsh.model.occ.synchronize()

    # Get the children of the aircraft parts

    aircraft_parts_children_dimtag = children_dimtag[1:]

    log.info("Before/after fragment operation relations:")
    for parent, children in zip(aircraft_parts, aircraft_parts_children_dimtag):

        # don't assign unwanted children if symmetry was used

        children = [child for child in children if child not in unwanted_children]

        log.info(f"{parent.uid} has generated {children} children")
        parent.children_dimtag = set(children)

    # Some parent may have no children (due to symmetry), we need to remove them
    for parent in aircraft_parts:
        if not parent.children_dimtag:
            log.info(f"{parent.uid} has no more children due to symmetry, it will be deleted")
            aircraft_parts.remove(parent)

    # Process and add children that are shared by two parent parts in the shared children list
    # and put them in a new unwanted children list

    unwanted_children = []

    for part in aircraft_parts:
        for other_part in aircraft_parts:

            if part != other_part:
                shared_children = part.children_dimtag.intersection(other_part.children_dimtag)

                if part.children_dimtag.intersection(other_part.children_dimtag):
                    part.children_dimtag = part.children_dimtag - shared_children
                    other_part.children_dimtag = other_part.children_dimtag - shared_children

                unwanted_children.extend(list(shared_children))

    # remove duplicated from the unwanted child list
    unwanted_children = list(set(unwanted_children))

    # and remove them from the model
    gmsh.model.occ.remove(unwanted_children, recursive=True)
    gmsh.model.occ.synchronize()
    log.info(f"Unwanted children {unwanted_children} removed from model")

    # Associate good child with their parent
    good_children = []

    for parent in aircraft_parts:
        for child_dimtag in parent.children_dimtag:
            if child_dimtag not in unwanted_children:

                good_children.append(child_dimtag)
                log.info(f"Associating child {child_dimtag} to parent {parent.uid}")
                parent.associate_child_to_parent(child_dimtag)

    # Now that its clear which child entities in the model are from which parent part,
    # we can delete the child volumes and only keep the final domain
    gmsh.model.occ.remove(good_children, recursive=True)
    gmsh.model.occ.synchronize()

    # Now only the final domain is left, in the model, we can find its entities
    # we will use the ModelPart class to store the entities of the final domain
    final_domain = ModelPart("fluid")
    left_volume = gmsh.model.getEntities(dim=3)
    final_domain.associate_child_to_parent(*left_volume)

    # As already discussed, it is often that two parts intersect each other,
    # it can also happend that some parts create holes inside other parts
    # for example a fuselage and 2 wings defined in the center of the fuselage
    # will create a holed fragment of the fuselage
    # This is not a problem since this hole is not in the final domain volume
    # but they may be some lines and surfaces from the hole in the fuselage
    # that were not eliminated since they were shared by the unwanted children
    # and those lines and surfaces were assigned to the fuselage part

    # thus we need to clean a bit the associated entities by the function
    # associate_child_to_parent() by comparing them with the entities of the
    # final domain

    # Create an aircraft part containing all the parts of the aircraft
    aircraft = ModelPart("aircraft")

    for part in aircraft_parts:
        part.clean_inside_entities(final_domain)

        aircraft.points.extend(part.points)
        aircraft.lines.extend(part.lines)
        aircraft.surfaces.extend(part.surfaces)
        aircraft.volume.extend(part.volume)
        aircraft.points_tags.extend(part.points_tags)
        aircraft.lines_tags.extend(part.lines_tags)
        aircraft.surfaces_tags.extend(part.surfaces_tags)
        aircraft.volume_tag.extend(part.volume_tag)

        surfaces_group = gmsh.model.addPhysicalGroup(2, part.surfaces_tags)
        gmsh.model.setPhysicalName(2, surfaces_group, f"{part.uid}")

    log.info("Model has been cleaned")

    # Farfield
    # farfield entities are simply the entities left in the final domain
    # that don't belong to the aircraft

    farfield_surfaces = list(set(final_domain.surfaces) - set(aircraft.surfaces))
    farfield_points = list(set(final_domain.points) - set(aircraft.points))
    farfield_surfaces_tags = list(set(final_domain.surfaces_tags) - set(aircraft.surfaces_tags))

    if symmetry:

        symmetry_surfaces = []
        symmetry_surfaces_tags = []

        # If symmetry was used, it means that in the farfield entities we have
        # a surface that is the plane of symmetry, we need to find it
        # and remove it from the farfield entities

        # In general it is easy because the symmetry plane should be the only surface
        # in the farfield who touch the aircraft

        for farfield_surface in farfield_surfaces:
            _, adj_lines_tags = gmsh.model.getAdjacencies(*farfield_surface)

            if set(adj_lines_tags).intersection(set(aircraft.lines_tags)):

                farfield_surfaces.remove(farfield_surface)
                farfield_surfaces_tags.remove(farfield_surface[1])

                symmetry_surfaces.append(farfield_surface)
                symmetry_surfaces_tags.append(farfield_surface[1])

        symmetry_group = gmsh.model.addPhysicalGroup(2, symmetry_surfaces_tags)
        gmsh.model.setPhysicalName(2, symmetry_group, "symmetry")

    farfield = gmsh.model.addPhysicalGroup(2, farfield_surfaces_tags)
    gmsh.model.setPhysicalName(2, farfield, "Farfield")

    # Fluid domain
    ps = gmsh.model.addPhysicalGroup(3, final_domain.volume_tag)
    gmsh.model.setPhysicalName(3, ps, final_domain.uid)

    gmsh.model.occ.synchronize()
    log.info("Markers for SU2 generated")

    # Mesh Generation

    # Set mesh size of the aircraft parts

    # not that points common between parts will have the size of the last part
    # to set its mesh size.
    # Thus be sure to define mesh size in a certain order to control
    # the size of the points on boundaries.

    for part in aircraft_parts:
        if "fuselage" in part.part_type:
            part.mesh_size = mesh_size_fuselage
            gmsh.model.mesh.setSize(part.points, part.mesh_size)
            gmsh.model.setColor(
                part.surfaces, *MESH_COLORS[part.part_type], a=100, recursive=False
            )
        elif part.part_type in ["wing", "pylon", "nacelle", "engine"]:
            part.mesh_size = mesh_size_wings
            gmsh.model.mesh.setSize(part.points, part.mesh_size)
            gmsh.model.setColor(
                part.surfaces, *MESH_COLORS[part.part_type], a=100, recursive=False
            )

    # Set mesh size and color of the farfield
    gmsh.model.mesh.setSize(farfield_points, mesh_size_farfield)
    gmsh.model.setColor(farfield_surfaces, *MESH_COLORS["farfield"], a=255, recursive=False)

    if symmetry:
        gmsh.model.setColor(symmetry_surfaces, *MESH_COLORS["symmetry"], a=150, recursive=False)

    # Generate advance meshing features
    if refine_factor != 1:
        mesh_fields = {"nbfields": 0, "restrict_fields": []}
        for part in aircraft_parts:
            if "wing" in part.part_type:

                # wing classifications
                classify_wing(part, aircraft_parts)
                log.info(
                    f"Classification of {part.uid} done"
                    f"{len(part.wing_sections)} section(s) found "
                )

                # wing refinement
                refine_wing_section(
                    mesh_fields,
                    final_domain.volume_tag,
                    aircraft,
                    part,
                    mesh_size_wings,
                    refine=refine_factor,
                )
            elif "fuselage" in part.part_type:
                set_fuselage_mesh(mesh_fields, part, mesh_size_fuselage)

        set_farfield_mesh(
            mesh_fields,
            aircraft_parts,
            mesh_size_farfield,
            max(model_dimensions),
            final_domain.volume_tag,
        )

        # Generate the minimal background mesh field
        mesh_fields["nbfields"] += 1
        gmsh.model.mesh.field.add("Min", mesh_fields["nbfields"])
        gmsh.model.mesh.field.setNumbers(
            mesh_fields["nbfields"], "FieldsList", mesh_fields["restrict_fields"]
        )
        gmsh.model.mesh.field.setAsBackgroundMesh(mesh_fields["nbfields"])

        # When background mesh is used those options must be set to zero
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)

    # Mesh generation
    log.info("Start of gmsh 2D surface meshing process")
    gmsh.model.occ.synchronize()
    gmsh.model.mesh.generate(1)
    gmsh.model.mesh.generate(2)

    # Apply smoothing

    gmsh.model.mesh.optimize("Laplace2D", niter=1)

    if open_gmsh:
        log.info("Result of 2D surface mesh")
        log.info("GMSH GUI is open, close it to continue...")
        gmsh.fltk.run()

    log.info("Start of gmsh 3D volume meshing process")
    gmsh.logger.start()
    gmsh.model.mesh.generate(3)
    gmsh.model.occ.synchronize()

    su2mesh_path = Path(results_dir, "mesh.su2")
    gmsh.write(str(su2mesh_path))

    process_gmsh_log(gmsh.logger.get())
    if open_gmsh:
        log.info("Result of the 3D volume mesh")
        log.info("GMSH GUI is open, close it to continue...")
        gmsh.fltk.run()
    gmsh.clear()
    gmsh.finalize()
    return su2mesh_path, aircraft_parts


# =================================================================================================
#    MAIN
# =================================================================================================

if __name__ == "__main__":

    print("Nothing to execute!")
