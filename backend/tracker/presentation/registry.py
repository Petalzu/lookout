"""Registry for reading documents; no collection or persistence."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracker.presentation.activity import sections as activity_sections
from tracker.presentation.build import build_cells, build_columns, build_sections
from tracker.presentation.commits import cells as commits_cells
from tracker.presentation.commits import sections as commits_sections
from tracker.presentation.evidence import evidence_cells, evidence_lines, evidence_section
from tracker.presentation.model import Cell, Column, Section, Text
from tracker.presentation.requires import requires_cells, requires_preview, requires_sections
from tracker.presentation.source import source_sections
from tracker.presentation.values import single_column
from tracker.presentation.version import version_cells, version_sections

if TYPE_CHECKING:
    from tracker.presentation.navigation import Links


@dataclass(frozen=True)
class Presenter:
    """Package context is always composable; a results view is opt-in.

    A shared observation interface does not imply a separate navigation entry.
    Columns and cells must answer an independent list-reading question together.
    """
    sections: Callable[[dict, Links], list[Section]]
    columns: Callable[[str, list[dict]], list[Column]] | None = None
    cells: Callable[[dict, dict, Links], list[Cell]] | None = None
    preview: Callable[[dict, dict, Links], list[list[Text]]] | None = None

    def __post_init__(self):
        if (self.columns is None) != (self.cells is None):
            raise ValueError('a results view requires both columns and cells')

    @property
    def has_results(self):
        return self.columns is not None


PRESENTERS = {
    'activity': Presenter(activity_sections),
    'commits': Presenter(commits_sections, single_column, commits_cells),
    'source': Presenter(source_sections),
    'version': Presenter(version_sections, single_column, version_cells),
    'build': Presenter(build_sections, build_columns, build_cells),
    'evidence': Presenter(evidence_section, single_column, evidence_cells, preview=evidence_lines),
    'requires': Presenter(requires_sections, single_column, requires_cells, preview=requires_preview),
}


def presenter(descriptor):
    return PRESENTERS[descriptor['kind']]
