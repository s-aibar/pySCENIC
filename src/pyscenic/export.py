# coding=utf-8

import os
import json
import numpy as np
import pandas as pd
import loompy as lp
from sklearn.manifold.t_sne import TSNE
from .aucell import aucell
from .genesig import Regulon
from typing import List, Mapping, Sequence, Optional
from operator import attrgetter
from multiprocessing import cpu_count
from .binarization import binarize
from itertools import chain, repeat, islice
import networkx as nx


def export2loom(ex_mtx: pd.DataFrame, regulons: List[Regulon], cell_annotations: Mapping[str,str],
                out_fname: str,
                tree_structure: Sequence[str] = (),
                title: Optional[str] = None,
                nomenclature: str = "Unknown",
                num_workers=cpu_count(),
                tsne_embedding_mtx=None, auc_mtx=None, auc_thresholds=None):
    """
    Create a loom file for a single cell experiment to be used in SCope.

    :param ex_mtx: The expression matrix (n_cells x n_genes).
    :param regulons: A list of Regulons.
    :param cell_annotations: A dictionary that maps a cell ID to its corresponding cell type annotation.
    :param out_fname: The name of the file to create.
    :param tree_structure: A sequence of strings that defines the category tree structure.
    :param title: The title for this loom file. If None than the basename of the filename is used as the title.
    :param nomenclature: The name of the genome.
    :param num_workers: The number of cores to use for AUCell regulon enrichment.
    """
    # Information on the general loom file format: http://linnarssonlab.org/loompy/format/index.html
    # Information on the SCope specific alterations: https://github.com/aertslab/SCope/wiki/Data-Format

    # Calculate regulon enrichment per cell using AUCell.
    if auc_mtx is None:
        auc_mtx = aucell(ex_mtx, regulons, num_workers=num_workers) # (n_cells x n_regulons)
    # Binarize matrix for AUC thresholds.
    if auc_thresholds is None:
        _, auc_thresholds = binarize(auc_mtx)

    # Create an embedding based on tSNE.
    # Name of columns should be "_X" and "_Y".
    if tsne_embedding_mtx is None:
        tsne_embedding_mtx = TSNE().fit_transform(auc_mtx)

    tsne_embedding_mtx = pd.DataFrame(data=tsne_embedding_mtx,
                                      index=ex_mtx.index, columns=['_X', '_Y']) # (n_cells, 2)
    
    # Calculate the number of genes per cell.
    binary_mtx = ex_mtx.copy()
    binary_mtx[binary_mtx != 0] = 1.0
    ngenes = binary_mtx.sum(axis=1).astype(int)

    # Encode genes in regulons as "binary" membership matrix.
    genes = np.array(ex_mtx.columns)
    n_genes = len(genes)
    n_regulons = len(regulons)
    data = np.zeros(shape=(n_genes, n_regulons), dtype=int)
    for idx, regulon in enumerate(regulons):
        data[:, idx] = np.isin(genes, regulon.genes).astype(int)
    regulon_assignment = pd.DataFrame(data=data,
                                      index=ex_mtx.columns,
                                      columns=list(map(attrgetter('name'), regulons)))

    # Encode cell type clusters.
    # The name of the column should match the identifier of the clustering.
    name2idx = dict(map(reversed, enumerate(sorted(set(cell_annotations.values())))))
    clusterings = pd.DataFrame(data=ex_mtx.index,
                               index=ex_mtx.index,
                               columns=['0']).replace(cell_annotations).replace(name2idx)

    # Create meta-data structure.
    def create_structure_array(df):
        # Create a numpy structured array
        return np.array([tuple(row) for row in df.values],
                        dtype=np.dtype(list(zip(df.columns, df.dtypes))))

    column_attrs = {
        "CellID": ex_mtx.index.values.astype('str'),
        "nGene": ngenes.values,
        "Embedding": create_structure_array(tsne_embedding_mtx),
        "RegulonsAUC": create_structure_array(auc_mtx),
        "Clusterings": create_structure_array(clusterings),
        "ClusterID": clusterings.values
        }
    row_attrs = {
        "Gene": ex_mtx.columns.values.astype('str'),
        "Regulons": create_structure_array(regulon_assignment),
        }

    def fetch_logo(context):
        for elem in context:
            if elem.endswith('.png'):
                return elem
        return ""
    name2logo = {reg.name: fetch_logo(reg.context) for reg in regulons}
    regulon_thresholds = [{"regulon": name,
                            "defaultThresholdValue":(threshold if isinstance(threshold, float) else threshold[0]),
                            "defaultThresholdName": "guassian_mixture_split",
                            "allThresholds": {"guassian_mixture_split": (threshold if isinstance(threshold, float) else threshold[0])},
                            "motifData": name2logo.get(name, "")} for name, threshold in auc_thresholds.iteritems()]

    general_attrs = {
        "title": os.path.splitext(os.path.basename(out_fname))[0] if title is None else title,
        "MetaData": json.dumps({
            "embeddings": [{
                "id": 0,
                "name": "tSNE (default)",
            }],
            "annotations": [{
                "name": "",
                "values": []
            }],
            "clusterings": [{
                "id": 0,
                "group": "celltype",
                "name": "Cell Type",
                "clusters": [{"id": idx, "description": name} for name, idx in name2idx.items()]
            }],
            "regulonThresholds": regulon_thresholds
        }),
        "Genome": nomenclature}

    # Add tree structure.
    # All three levels need to be supplied
    assert len(tree_structure) <= 3, ""
    general_attrs.update(("SCopeTreeL{}".format(idx+1), category)
                         for idx, category in enumerate(list(islice(chain(tree_structure, repeat("")), 3))))

    # Create loom file for use with the SCope tool.
    # The loom file format opted for rows as genes to facilitate growth along the column axis (i.e add more cells)
    # PySCENIC chose a different orientation because of limitation set by the feather format: selectively reading
    # information from disk can only be achieved via column selection. For the ranking databases this is of utmost
    # importance.
    fh = lp.create(filename=out_fname,
              matrix=ex_mtx.T.values,
              row_attrs=row_attrs,
              col_attrs=column_attrs,
              file_attrs=general_attrs)
    fh.close()


def export_regulons(regulons: Sequence[Regulon], fname: str) -> None:
    """

    Export regulons as GraphML.

    :param regulons: The sequence of regulons to export.
    :param fname: The name of the file to create.
    """
    graph = nx.DiGraph()
    for regulon in regulons:
        src_name = regulon.transcription_factor
        graph.add_node(src_name, group='transcription_factor')
        edge_type = 'activating' if 'activating' in regulon.context else 'inhibiting'
        node_type = 'activated_target' if 'activating' in regulon.context else 'inhibited_target'
        for dst_name, edge_strength in regulon.gene2weight.items():
            graph.add_node(dst_name, group=node_type, **regulon.context)
            graph.add_edge(src_name, dst_name, weight=edge_strength, interaction=edge_type, **regulon.context)
    nx.readwrite.write_graphml(graph, fname)
