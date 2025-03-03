__version__ = "1"

import json
import time
import tarfile
import tifffile

import pandas as pd
import numpy as np
import multiprocessing as mp

from rich.progress import track
from pathlib import Path
from scipy.ndimage import zoom
from allensdk.core.reference_space_cache import ReferenceSpaceCache

# import sys

# sys.path.append("./")

from bg_atlasapi import utils
from bg_atlasgen.mesh_utils import create_region_mesh, Region
from bg_atlasgen.wrapup import wrapup_atlas_from_data
from bg_atlasapi.structure_tree_util import get_structures_tree

PARALLEL = False  # disable parallel mesh extraction for easier debugging


def create_atlas(working_dir, resolution):
    ATLAS_NAME = "KimLabDevCCF"
    SPECIES = "Mus musculus"
    ATLAS_LINK = "https://data.mendeley.com/datasets/2svx788ddf/1"
    CITATION = "Kim, Yongsoo (2022), “KimLabDevCCFv001”, Mendeley Data, V1, doi: 10.17632/2svx788ddf.1"
    ORIENTATION = "asl"
    ROOT_ID = 15565
    ANNOTATIONS_RES_UM = 10
    ATLAS_FILE_URL = "https://md-datasets-cache-zipfiles-prod.s3.eu-west-1.amazonaws.com/2svx788ddf-1.zip"

    # Temporary folder for  download:
    download_dir_path = working_dir / "downloads"
    download_dir_path.mkdir(exist_ok=True)
    atlas_files_dir = download_dir_path / "atlas_files"

    # Download atlas_file
    utils.check_internet_connection()

    destination_path = download_dir_path / "atlas_download"
    utils.retrieve_over_http(ATLAS_FILE_URL, destination_path)

    tar = tarfile.open(destination_path)
    tar.extractall(path=atlas_files_dir)
    tar.close()

    destination_path.unlink()

    structures_file = atlas_files_dir / "KimLabDevCCF" / "KimLabDevCCFv001_MouseOntologyStructure.csv"
    annotations_file = atlas_files_dir / "KimLabDevCCF" / "10um" / "KimLabDevCCFv001_Annotations_ASL_Oriented_10um.nii.gz"

    # ---------------------------------------------------------------------------- #
    #                                 GET TEMPLATE                                 #
    # ---------------------------------------------------------------------------- #

    # Load (and possibly downsample) annotated volume:
    scaling = ANNOTATIONS_RES_UM / resolution

    annotated_volume = tifffile.imread(annotations_file)
    annotated_volume = zoom(
        annotated_volume, (scaling, scaling, scaling), order=0, prefilter=False
    )

    # Download annotated and template volume:
    #########################################
    spacecache = ReferenceSpaceCache(
        manifest=download_dir_path / "manifest.json",
        # downloaded files are stored relative to here
        resolution=resolution,
        reference_space_key="annotation/ccf_2017"
        # use the latest version of the CCF
    )

    # Download
    template_volume, _ = spacecache.get_template_volume()
    print("Download completed...")

    # ---------------------------------------------------------------------------- #
    #                             STRUCTURES HIERARCHY                             #
    # ---------------------------------------------------------------------------- #

    # Parse region names & hierarchy
    # ##############################
    df = pd.read_csv(structures_file)
    df = df.drop(columns=["Unnamed: 0", "Parent ID", "parent_acronym"])

    # split by "/" and convert list of strings to list of ints
    df["structure_id_path"] = (
        df["structure_id_path"]
        .str.split(pat="/")
        .map(lambda x: [int(i) for i in x])
    )

    structures = df.to_dict("records")

    for structure in structures:
        structure.update({"rgb_triplet": [255, 255, 255]})
        # root doesn't have a parent
        if structure["id"] != 15565:
            structure["structure_id_path"].append(structure["id"])

    # save regions list json:
    with open(download_dir_path / "structures.json", "w") as f:
        json.dump(structures, f)

    # Create meshes:
    print(f"Saving atlas data at {download_dir_path}")
    meshes_dir_path = download_dir_path / "meshes"
    meshes_dir_path.mkdir(exist_ok=True)

    tree = get_structures_tree(structures)

    rotated_annotations = np.rot90(annotated_volume, axes=(0, 2))

    labels = np.unique(rotated_annotations).astype(np.int32)
    for key, node in tree.nodes.items():
        if key in labels:
            is_label = True
        else:
            is_label = False

        node.data = Region(is_label)

    # Mesh creation
    closing_n_iters = 2
    decimate_fraction = 0.2
    smooth = False  # smooth meshes after creation

    start = time.time()
    if PARALLEL:

        pool = mp.Pool(mp.cpu_count() - 2)

        try:
            pool.map(
                create_region_mesh,
                [
                    (
                        meshes_dir_path,
                        node,
                        tree,
                        labels,
                        rotated_annotations,
                        ROOT_ID,
                        closing_n_iters,
                        decimate_fraction,
                        smooth,
                    )
                    for node in tree.nodes.values()
                ],
            )
        except mp.pool.MaybeEncodingError:
            pass  # error with returning results from pool.map but we don't care
    else:
        for node in track(
            tree.nodes.values(),
            total=tree.size(),
            description="Creating meshes",
        ):
            create_region_mesh(
                (
                    meshes_dir_path,
                    node,
                    tree,
                    labels,
                    rotated_annotations,
                    ROOT_ID,
                    closing_n_iters,
                    decimate_fraction,
                    smooth,
                )
            )

    print(
        "Finished mesh extraction in: ",
        round((time.time() - start) / 60, 2),
        " minutes",
    )

    # Create meshes dict
    meshes_dict = dict()
    structures_with_mesh = []
    for s in structures:
        # Check if a mesh was created
        mesh_path = meshes_dir_path / f'{s["id"]}.obj'
        if not mesh_path.exists():
            print(f"No mesh file exists for: {s}, ignoring it")
            continue
        else:
            # Check that the mesh actually exists (i.e. not empty)
            if mesh_path.stat().st_size < 512:
                print(f"obj file for {s} is too small, ignoring it.")
                continue

        structures_with_mesh.append(s)
        meshes_dict[s["id"]] = mesh_path

    print(
        f"In the end, {len(structures_with_mesh)} structures with mesh are kept"
    )

    # ---------------------------------------------------------------------------- #
    #                                    WRAP UP                                   #
    # ---------------------------------------------------------------------------- #

    # Wrap up, compress, and remove file:
    print("Finalising atlas")
    output_filename = wrapup_atlas_from_data(
        atlas_name=ATLAS_NAME,
        atlas_minor_version=__version__,
        citation=CITATION,
        atlas_link=ATLAS_LINK,
        species=SPECIES,
        resolution=(resolution,) * 3,
        orientation=ORIENTATION,
        root_id=ROOT_ID,
        reference_stack=template_volume,
        annotation_stack=annotated_volume,
        structures_list=structures_with_mesh,
        meshes_dict=meshes_dict,
        working_dir=working_dir,
        hemispheres_stack=None,
        cleanup_files=False,
        compress=True,
        scale_meshes=True,
    )

    return output_filename


if __name__ == "__main__":
    resolution = 10  # some resolution, in microns (10, 25, 50, 100)

    # Generated atlas path:
    bg_root_dir = Path.home() / "brainglobe_workingdir" / "kim_mouse"
    bg_root_dir.mkdir(exist_ok=True, parents=True)
    create_atlas(bg_root_dir, resolution)
