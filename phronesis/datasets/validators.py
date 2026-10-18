"""
Structural validation of uploaded files. Uploaded content is only parsed as data: nothing is executed,
imported or unpickled, HDF5 links that point outside the file are rejected, and SBML is parsed with
DTDs, entities and external references forbidden before COPASI ever sees it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import ValidationSummary

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
MIN_OBS = 5
MIN_VARS = 2
MAX_CELLS_X_GENES = 2_000_000_000
MAX_TABLE_ROWS = 200_000
MAX_TABLE_COLUMNS = 100_000
MAX_HDF5_OBJECTS = 200_000  # groups + datasets + links an .h5ad may hold (an AnnData file has a few hundred)
MAX_ZARR_FILES = 200_000
MAX_SBML_ELEMENTS = 500_000  # XML elements in one SBML document (the upload cap is 10 MB)

_SBML_NS_PREFIX = "{http://www.sbml.org/sbml/"
_XINCLUDE_NS = "{http://www.w3.org/2001/XInclude}"

# Advisory only: obs column names that often hold direct identifiers.
_PHI_COLUMN_RE = re.compile(
    r"(?:^|[_\W])(name|first_?name|last_?name|surname|dob|birth_?date|date_?of_?birth|mrn|ssn|"
    r"social_?security|address|street|phone|email|zip_?code|postcode)(?:$|[_\W])",
    re.IGNORECASE,
)
_UNS_HINT_KEYS = ("organism", "species", "tissue", "disease", "technology", "assay")


class ValidationFailure(Exception):
    """A file that cannot be accepted; reason_code is stable and shown to the uploader."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _check_dims(n_obs: int, n_vars: int) -> None:
    if n_obs < MIN_OBS:
        raise ValidationFailure("too_few_cells", f"The matrix has {n_obs} cells; at least {MIN_OBS} are needed.")
    if n_vars < MIN_VARS:
        raise ValidationFailure("too_few_genes", f"The matrix has {n_vars} genes; at least {MIN_VARS} are needed.")
    if n_obs * n_vars > MAX_CELLS_X_GENES:
        raise ValidationFailure(
            "matrix_too_large", f"{n_obs} cells × {n_vars} genes exceeds the {MAX_CELLS_X_GENES:.0e} cell-gene limit."
        )


def _phi_warnings(columns: List[str]) -> List[str]:
    return [
        f"obs column '{c}' may contain direct identifiers; confirm it is de-identified"
        for c in columns
        if _PHI_COLUMN_RE.search(c)
    ]


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# -----------------------------------------------------------------------------
# HDF5 / zarr structure limits (SEC-010)
# -----------------------------------------------------------------------------
# libhdf5, COPASI/libSBML and zarr still parse untrusted files inside the API process (no subprocess isolation and no
# rlimits); these checks bound what a hostile file can make the parser do (links out of the file, object-count bombs,
# allocations far beyond the declared caps) but cannot protect against a memory-safety bug in the native libraries.
# Keep h5py/libhdf5 and basico/COPASI patched; parsing in a resource-limited subprocess is the remaining hardening.

def check_hdf5_structure(f: object, max_objects: int = MAX_HDF5_OBJECTS) -> int:
    """Reject HDF5 soft/external links, virtual or externally stored datasets and object-count bombs.

    Returns the number of stored elements over all datasets (a bound on what loading the file allocates)."""
    import h5py

    offending: List[str] = []
    count = 0

    def check_link(name: str, link: object) -> Optional[bool]:
        nonlocal count
        count += 1
        if count > max_objects:
            return True  # stop the traversal
        if isinstance(link, (h5py.ExternalLink, h5py.SoftLink)):
            offending.append(f"{name} ({type(link).__name__})")
        return None

    try:
        f.visititems_links(check_link)  # type: ignore[attr-defined]
    except Exception as exc:
        raise ValidationFailure("hdf5_unreadable", f"The HDF5 structure could not be traversed ({type(exc).__name__}).") from exc
    if count > max_objects:
        raise ValidationFailure("hdf5_too_complex", f"The HDF5 file has more than {max_objects:,} objects.")
    if offending:
        raise ValidationFailure(
            "hdf5_external_link",
            f"The file contains HDF5 links, which are not allowed in uploads: {', '.join(offending[:5])}.",
        )

    elements = 0

    def check_dataset(name: str, obj: object) -> None:
        nonlocal elements
        if isinstance(obj, h5py.Dataset):
            if obj.is_virtual or obj.external:
                offending.append(name)
            elements += int(obj.size or 0)

    f.visititems(check_dataset)  # type: ignore[attr-defined]
    if offending:
        raise ValidationFailure(
            "hdf5_external_link",
            f"The file contains virtual or externally stored datasets, which are not allowed: {', '.join(offending[:5])}.",
        )
    return elements


def preflight_h5ad(path: Path) -> Dict[str, int]:
    """Cheap structural read of an .h5ad before it is loaded for analysis (no cell x gene cap: large files are
    subsampled by the engine). Returns n_obs, n_vars and stored_elements; raises ValidationFailure."""
    import h5py

    with open(path, "rb") as fh:
        if fh.read(8) != HDF5_SIGNATURE:
            raise ValidationFailure("not_hdf5", "Not an HDF5/AnnData file (missing HDF5 signature).")
    try:
        f = h5py.File(path, "r")
    except Exception as exc:
        raise ValidationFailure("hdf5_unreadable", f"The HDF5 file could not be read; it may be truncated ({type(exc).__name__}).") from exc
    try:
        elements = check_hdf5_structure(f)
        if "X" not in f:
            raise ValidationFailure("not_anndata", "The AnnData file has no X matrix.")
        x = f["X"]
        if isinstance(x, h5py.Dataset):
            shape = tuple(int(s) for s in x.shape)
        else:
            raw_shape = x.attrs.get("shape", x.attrs.get("h5sparse_shape"))
            if raw_shape is None:
                raise ValidationFailure("not_anndata", "The sparse X matrix has no shape attribute.")
            shape = tuple(int(s) for s in raw_shape)
        if len(shape) != 2:
            raise ValidationFailure("not_anndata", f"X must be a 2-D matrix, found shape {shape}.")
        return {"n_obs": shape[0], "n_vars": shape[1], "stored_elements": int(elements)}
    finally:
        f.close()


def _zarr_shape(meta_dir: Path) -> Optional[Tuple[int, ...]]:
    """Shape of an AnnData X stored in zarr v2 (.zarray / .zattrs) or v3 (zarr.json)."""
    import json

    for name, keys in ((".zarray", ("shape",)), (".zattrs", ("shape", "h5sparse_shape")), ("zarr.json", ("shape",))):
        meta = meta_dir / name
        if not meta.is_file() or meta.stat().st_size > 1_000_000:
            continue
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for container in (doc, doc.get("attributes") or {}):
            for key in keys:
                value = container.get(key) if isinstance(container, dict) else None
                if isinstance(value, list) and all(isinstance(v, int) for v in value):
                    return tuple(value)
    return None


def preflight_zarr(path: Path, max_files: int = MAX_ZARR_FILES) -> Dict[str, int]:
    """Structural checks of an AnnData .zarr directory: no links leaving it, a bounded file count and a readable X
    shape. Returns n_obs, n_vars and the total stored bytes; raises ValidationFailure."""
    import os

    if not path.is_dir():
        raise ValidationFailure("not_anndata", "A .zarr dataset must be a directory store.")
    root = os.path.normcase(os.path.realpath(path))
    n_files = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                real = os.path.normcase(os.path.realpath(full))
                try:
                    inside = os.path.commonpath([real, root]) == root
                except ValueError:
                    inside = False
                if not inside:
                    raise ValidationFailure("zarr_external_link", f"The zarr store links outside itself ({name}).")
        n_files += len(filenames)
        if n_files > max_files:
            raise ValidationFailure("zarr_too_complex", f"The zarr store has more than {max_files:,} files.")
        for name in filenames:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    shape = _zarr_shape(path / "X")
    if shape is None or len(shape) != 2:
        raise ValidationFailure("not_anndata", "The zarr store has no recognisable 2-D AnnData X matrix.")
    return {"n_obs": int(shape[0]), "n_vars": int(shape[1]), "stored_bytes": int(total_bytes)}


# -----------------------------------------------------------------------------
# h5ad
# -----------------------------------------------------------------------------

def validate_h5ad(path: Path) -> ValidationSummary:
    import h5py

    with open(path, "rb") as fh:
        if fh.read(8) != HDF5_SIGNATURE:
            raise ValidationFailure("not_hdf5", "Not an HDF5/AnnData file (missing HDF5 signature).")
    try:
        f = h5py.File(path, "r")
    except Exception as exc:  # truncated or corrupt
        raise ValidationFailure("hdf5_unreadable", f"The HDF5 file could not be read; it may be truncated ({type(exc).__name__}).") from exc

    warnings: List[str] = []
    try:
        check_hdf5_structure(f)

        encoding = _decode(f.attrs.get("encoding-type", ""))
        missing = [k for k in ("obs", "var", "X") if k not in f]
        if encoding != "anndata":
            if encoding or missing:
                raise ValidationFailure("not_anndata", "The HDF5 file is not an AnnData (.h5ad) file.")
            warnings.append("legacy AnnData layout (no encoding-type attribute)")
        elif missing:
            raise ValidationFailure("not_anndata", f"The AnnData file is missing {', '.join(missing)}.")

        x = f["X"]
        if isinstance(x, h5py.Dataset):
            shape = tuple(x.shape)
            x_encoding = _decode(x.attrs.get("encoding-type", "array"))
        else:
            raw_shape = x.attrs.get("shape", x.attrs.get("h5sparse_shape"))
            if raw_shape is None:
                raise ValidationFailure("not_anndata", "The sparse X matrix has no shape attribute.")
            shape = tuple(int(s) for s in raw_shape)
            x_encoding = _decode(x.attrs.get("encoding-type", x.attrs.get("h5sparse_format", "sparse")))
            if x_encoding in ("csr", "csc"):
                x_encoding = f"{x_encoding}_matrix"
        if len(shape) != 2:
            raise ValidationFailure("not_anndata", f"X must be a 2-D matrix, found shape {shape}.")
        n_obs, n_vars = int(shape[0]), int(shape[1])
        _check_dims(n_obs, n_vars)

        obs = f["obs"]
        columns: List[str] = []
        if isinstance(obs, h5py.Group):
            order = obs.attrs.get("column-order")
            if order is not None:
                columns = [_decode(c) for c in list(order)][:200]
            else:
                index_key = _decode(obs.attrs.get("_index", "_index"))
                columns = [k for k in obs.keys() if k != index_key][:200]
        warnings.extend(_phi_warnings(columns))

        hints: Dict[str, str] = {}
        uns = f.get("uns")
        if isinstance(uns, h5py.Group):
            for key in _UNS_HINT_KEYS:
                item = uns.get(key)
                if isinstance(item, h5py.Dataset) and item.shape == () and item.dtype.kind in ("S", "O", "U"):
                    hints[key] = _decode(item[()])[:120]
        return ValidationSummary(
            format="anndata-h5ad",
            n_obs=n_obs,
            n_vars=n_vars,
            x_encoding=x_encoding,
            obs_columns=columns,
            uns_hints=hints,
            warnings=warnings,
        )
    finally:
        f.close()


# -----------------------------------------------------------------------------
# csv / tsv -> canonical data.h5ad
# -----------------------------------------------------------------------------

# Label shapes used to check a table's orientation (SCI-016). Barcodes / cell ids are tested first because 10x
# barcodes such as AAACATACAACCAC-1 also look like gene symbols.
_BARCODE_RE = re.compile(r"^[ACGTN]{8,}(?:[-_.][A-Za-z0-9]+)*$", re.IGNORECASE)
_CELL_ID_RE = re.compile(r"^(?:cells?|bc|barcodes?|obs|c)[_\-.:]?\d+$|^[A-Za-z0-9]+[_:][ACGTN]{8,}(?:-\d+)?$", re.IGNORECASE)
_GENE_RE = re.compile(
    r"^(?:ENS[A-Z]*G\d{6,}(?:\.\d+)?|MT-[A-Za-z0-9]+|[A-Za-z][A-Za-z0-9]{0,14}(?:[-.][A-Za-z0-9]{1,12}){0,2}|"
    r"[A-Za-z][A-Za-z0-9]*orf\d+)$"
)
_ORIENTATION_SAMPLE = 500


def _label_fractions(labels: List[str]) -> Tuple[float, float]:
    """(fraction gene-like, fraction cell-id-like) over an evenly spaced sample of labels."""
    if not labels:
        return 0.0, 0.0
    step = max(1, len(labels) // _ORIENTATION_SAMPLE)
    sample = labels[::step][:_ORIENTATION_SAMPLE]
    cells = sum(1 for x in sample if _BARCODE_RE.match(x) or _CELL_ID_RE.match(x))
    genes = sum(1 for x in sample if not (_BARCODE_RE.match(x) or _CELL_ID_RE.match(x)) and _GENE_RE.match(x))
    return genes / len(sample), cells / len(sample)


def infer_table_orientation(row_names: List[str], col_names: List[str]) -> Tuple[Optional[str], str]:
    """('genes_by_cells' | 'cells_by_genes' | None, basis) from the shape of the row and column labels."""
    row_gene, row_cell = _label_fractions(row_names)
    col_gene, col_cell = _label_fractions(col_names)
    basis = (
        f"rows: {row_gene:.0%} gene-like, {row_cell:.0%} cell-id-like; "
        f"columns: {col_gene:.0%} gene-like, {col_cell:.0%} cell-id-like"
    )
    if (row_gene >= 0.6 and col_cell >= 0.6) or (row_gene >= 0.8 and col_gene <= 0.2 and row_cell <= 0.1):
        return "genes_by_cells", basis
    if (row_cell >= 0.6 and col_gene >= 0.6) or (col_gene >= 0.8 and row_gene <= 0.2 and col_cell <= 0.1):
        return "cells_by_genes", basis
    return None, basis


def validate_table(path: Path, extension: str, orientation: Optional[str], out_path: Path) -> Tuple[ValidationSummary, str]:
    """Parse a numeric expression matrix and write it once as AnnData; returns (summary, sha256 of data.h5ad)."""
    import numpy as np
    import pandas as pd

    sep = "\t" if extension == ".tsv" else ","
    try:
        header = pd.read_csv(path, sep=sep, index_col=0, nrows=0, engine="c")
    except Exception as exc:
        raise ValidationFailure("table_unreadable", f"The table could not be parsed ({type(exc).__name__}).") from exc
    if header.shape[1] > MAX_TABLE_COLUMNS:
        raise ValidationFailure("matrix_too_large", f"Tables are limited to {MAX_TABLE_COLUMNS:,} columns.")
    try:
        df = pd.read_csv(path, sep=sep, index_col=0, nrows=MAX_TABLE_ROWS + 1, engine="c")
    except Exception as exc:
        raise ValidationFailure("table_unreadable", f"The table could not be parsed ({type(exc).__name__}).") from exc
    if len(df) > MAX_TABLE_ROWS:
        raise ValidationFailure("matrix_too_large", f"Tables are limited to {MAX_TABLE_ROWS:,} rows.")
    if df.shape[1] == 0 or df.shape[0] == 0:
        raise ValidationFailure("table_unreadable", "The table has no data rows or columns.")
    non_numeric = [str(c) for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]
    if non_numeric:
        raise ValidationFailure(
            "non_numeric_values",
            f"Every value must be numeric; non-numeric column(s): {', '.join(non_numeric[:5])}.",
        )
    matrix = df.to_numpy(dtype="float64")
    if not np.all(np.isfinite(matrix)):
        raise ValidationFailure("non_finite_values", "The table contains empty, NaN or infinite values.")

    row_names = [str(i) for i in df.index]
    col_names = [str(c) for c in df.columns]
    warnings: List[str] = []
    inferred, basis = infer_table_orientation(row_names, col_names)
    if orientation and inferred and orientation != inferred:
        raise ValidationFailure(
            "orientation_mismatch",
            f"The table was declared {orientation.replace('_', ' ')} but its labels indicate "
            f"{inferred.replace('_', ' ')} ({basis}). Re-upload with orientation '{inferred}' if that is correct.",
        )
    if orientation is None:
        if inferred:
            orientation = inferred
            warnings.append(f"orientation not declared; inferred {inferred.replace('_', ' ')} from the labels ({basis})")
        else:
            orientation = "cells_by_genes"
            warnings.append("orientation not declared and not recognisable from the labels; assumed cells by genes")
    elif inferred is None:
        warnings.append(f"the declared orientation ({orientation.replace('_', ' ')}) could not be checked against the labels ({basis})")
    if orientation == "genes_by_cells":
        matrix = matrix.T
        obs_names, var_names = col_names, row_names
    else:
        obs_names, var_names = row_names, col_names
    n_obs, n_vars = matrix.shape
    _check_dims(int(n_obs), int(n_vars))

    import anndata as ad

    adata = ad.AnnData(
        X=matrix.astype(np.float32),
        obs=pd.DataFrame(index=pd.Index(obs_names, dtype=str)),
        var=pd.DataFrame(index=pd.Index(var_names, dtype=str)),
    )
    if not adata.obs_names.is_unique:
        adata.obs_names_make_unique()
        warnings.append("duplicate cell names were made unique")
    if not adata.var_names.is_unique:
        adata.var_names_make_unique()
        warnings.append("duplicate gene names were made unique")
    if bool(np.any(matrix < 0)):
        warnings.append("the matrix contains negative values; Lumicell expects raw or normalized counts")
    adata.write_h5ad(out_path)
    summary = ValidationSummary(
        format=f"{extension.lstrip('.')}->anndata",
        n_obs=int(n_obs),
        n_vars=int(n_vars),
        x_encoding="array",
        table_orientation=orientation,
        warnings=warnings,
    )
    return summary, sha256_file(out_path)


# -----------------------------------------------------------------------------
# SBML
# -----------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def validate_sbml(path: Path) -> ValidationSummary:
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import ParseError, fromstring

    raw = path.read_bytes()
    try:
        root = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise ValidationFailure("sbml_dtd_forbidden", "SBML with a DTD, entities or external references is not allowed.") from exc
    except ParseError as exc:
        raise ValidationFailure("xml_unparseable", f"The file is not well-formed XML ({exc}).") from exc
    except Exception as exc:
        raise ValidationFailure("xml_unparseable", f"The file is not well-formed XML ({type(exc).__name__}).") from exc

    if not (isinstance(root.tag, str) and root.tag.startswith(_SBML_NS_PREFIX) and _local(root.tag) == "sbml"):
        raise ValidationFailure("not_sbml", "The XML root is not an SBML <sbml> element.")
    for n_elements, el in enumerate(root.iter(), start=1):
        if n_elements > MAX_SBML_ELEMENTS:
            raise ValidationFailure("sbml_too_complex", f"The SBML document has more than {MAX_SBML_ELEMENTS:,} XML elements.")
        tag = el.tag if isinstance(el.tag, str) else ""
        if _local(tag) == "externalModelDefinition" or tag.startswith(_XINCLUDE_NS):
            raise ValidationFailure("sbml_external_reference", "External SBML model references are not supported.")

    ns = root.tag[: root.tag.index("}") + 1]
    model_el = root.find(f"{ns}model")
    if model_el is None:
        raise ValidationFailure("sbml_empty_model", "The SBML document has no <model>.")
    try:
        level = int(root.get("level", "0"))
        version = int(root.get("version", "0"))
    except ValueError:
        level = version = 0
    model_id = model_el.get("id")
    xml_name = model_el.get("name")

    text = raw.decode("utf-8", "replace")
    import basico

    from ..vivarix.engine import _COPASI_LOCK

    with _COPASI_LOCK:
        model = None
        try:
            try:
                model = basico.load_model_from_string(text)
            except Exception as exc:
                raise ValidationFailure("sbml_unloadable", f"COPASI could not load the model ({type(exc).__name__}).") from exc
            if model is None:
                raise ValidationFailure("sbml_unloadable", "COPASI could not load the model.")
            species = basico.get_species(model=model)
            n_species = 0 if species is None else len(species)
            params = basico.get_reaction_parameters(model=model)
            n_params = 0 if params is None else len(params)
            units = {k: str(v) for k, v in (basico.model_info.get_model_units(model=model) or {}).items() if v}
            name = basico.get_model_name(model=model) or xml_name
        finally:
            if model is not None:
                try:
                    basico.remove_datamodel(model)
                except Exception:  # noqa: BLE001
                    pass
    if n_species < 1:
        raise ValidationFailure("sbml_empty_model", "The SBML model defines no species to simulate.")
    return ValidationSummary(
        format=f"sbml-l{level}v{version}",
        sbml_level=level or None,
        sbml_version=version or None,
        sbml_model_id=model_id,
        sbml_model_name=(name or "")[:200] or None,
        n_species=n_species,
        n_reaction_parameters=n_params,
        units=units,
    )


def validate_dataset_upload(
    path: Path | str,
    original_filename: Optional[str] = None,
    *,
    orientation: Optional[str] = None,
    out_path: Optional[Path] = None,
) -> ValidationSummary:
    """Validate a file in any format the upload store accepts (.h5ad, .csv, .tsv, .xml, .sbml); SCI-017.

    Every other extension, including .zarr (not an upload format), raises ValidationFailure('unsupported_format').
    A table is converted to AnnData at out_path, or in a temporary directory that is removed afterwards."""
    p = Path(path)
    fn = original_filename or p.name
    suffix = Path(fn).suffix.lower()
    if suffix == ".h5ad":
        return validate_h5ad(p)
    if suffix in (".csv", ".tsv"):
        if out_path is not None:
            return validate_table(p, suffix, orientation, Path(out_path))[0]
        import tempfile

        with tempfile.TemporaryDirectory(prefix="phronesis_table_") as tmp:
            return validate_table(p, suffix, orientation, Path(tmp) / "data.h5ad")[0]
    if suffix in (".xml", ".sbml"):
        return validate_sbml(p)
    raise ValidationFailure(
        "unsupported_format", f"Unsupported file extension: {suffix or '(none)'}; upload .h5ad, .csv, .tsv, .xml or .sbml."
    )
