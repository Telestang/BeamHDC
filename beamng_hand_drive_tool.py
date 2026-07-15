from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

import beamng_hand_drive_core as core
from model_preview import ModelPreview

try:  # GPU mesh preview; the box viewer remains the fallback
    import mesh_preview
except Exception:
    mesh_preview = None


THIS_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BLENDER_PREVIEW_SCRIPT = RESOURCE_DIR / "blender_preview_backend.py"
APP_ICON_NAME = "BeamHDC_icon.ico"
BLENDER_CANDIDATES = (
    Path(r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"),
    Path(r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe"),
    Path(r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe"),
    Path(r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"),
)


MODEL_HISTORY_LIMIT = 12


def fmt_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def yn_label(value: object) -> str:
    return "Y" if bool(value) else "N"


def mode_label(mode: str) -> str:
    return {
        core.MODE_SKIP: "Skip",
        core.MODE_MIRROR: "Mirror Aesthetic",
        core.MODE_MIRROR_STRUCTURAL: "Mirror Structural",
        core.MODE_TRANSLATE: "Translate",
    }.get(mode, "Skip")


MODE_CYCLE_VALUES = [core.MODE_SKIP, core.MODE_MIRROR, core.MODE_MIRROR_STRUCTURAL, core.MODE_TRANSLATE]

# How long (in milliseconds) a part may sit on Mirror Structural before the
# source-part prompt commits it. Tweak this value to change the timeout.
STRUCTURAL_PROMPT_DELAY_MS = 1200


RECOMMEND_SIDE_PAIRS = (
    ("_fl", "_fr"),
    ("_fr", "_fl"),
    ("_rl", "_rr"),
    ("_rr", "_rl"),
    ("_frontleft", "_frontright"),
    ("_frontright", "_frontleft"),
    ("_rearleft", "_rearright"),
    ("_rearright", "_rearleft"),
    ("_left", "_right"),
    ("_right", "_left"),
    ("_driver", "_passenger"),
    ("_passenger", "_driver"),
    ("_l", "_r"),
    ("_r", "_l"),
    ("-l", "-r"),
    ("-r", "-l"),
    (".l", ".r"),
    (".r", ".l"),
)

RECOMMEND_TRANSLATE_PATTERNS = (
    r"digidash|digital_?dash|cluster|instrument",
    r"gauge|gauges|needle|speedo|tacho|tachometer",
    r"(?:gas|brake|clutch|throttle).*pedal|pedal.*(?:gas|brake|clutch|throttle)",
    r"pedalbox|pedal_box|padalbox",
    r"steer(?:ing)?_?wheel|steerwheel|(?:^|_)steer_[0-9]",
    r"paddle|signal_?stalk|wiper_?stalk",
)

RECOMMEND_TRANSLATE_EXCLUDE_PATTERNS = (
    r"footplate|(?:^|_)stand(?:_|$)|stand_plate",
)

RECOMMEND_MIRROR_PATTERNS = (
    r"dash|dashboard|console",
    r"parking_?brake|park_?brake|pbrake|hand_?brake|(?:^|_)hb_",
    r"shifter|shift_?knob|(?:^|_)grp_shift",
    r"radio|laptop|interior|headliner|sunvisor",
    r"steering_?column|(?:^|_)column(?:_|$)",
    r"intmirror|grp_mirror|hazard|dash_key|(?:^|_)key(?:_|$)",
    r"extinguisher|footplate|(?:^|_)stand(?:_|$)|stand_plate|cable",
)

RECOMMEND_STRUCTURAL_PATTERNS = (
    r"door_?panel",
    r"(?:^|_)mirror(?:_|$)|mirror_?stalk",
    r"(?:^|_)(?:race_?)?seats?(?:_|$)|racing_?seat",
)


def recommendation_text(context: core.VehicleContext, object_id: str) -> str:
    values = [object_id]
    obj = context.objects.get(object_id)
    if obj is not None and obj.name and obj.name != object_id:
        values.append(obj.name)
    return re.sub(r"[^a-z0-9]+", "_", " ".join(values).lower())


def recommendation_matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) is not None for pattern in patterns)


def recommendation_pair_candidate(object_id: str, candidates: set[str]) -> str | None:
    lower_to_id = {candidate.lower(): candidate for candidate in candidates}
    lowered = object_id.lower()
    for left, right in RECOMMEND_SIDE_PAIRS:
        if left not in lowered:
            continue
        candidate = lowered.replace(left, right, 1)
        if candidate in lower_to_id:
            return lower_to_id[candidate]
    return None


def build_mode_recommendations(
    context: core.VehicleContext,
    object_ids: list[str],
) -> list[dict[str, str]]:
    available = [object_id for object_id in object_ids if object_id in context.objects]
    candidate_set = set(available)
    paired: set[str] = set()
    recommendations: list[dict[str, str]] = []

    for object_id in available:
        if object_id in paired:
            continue
        text = recommendation_text(context, object_id)
        if not recommendation_matches(text, RECOMMEND_STRUCTURAL_PATTERNS):
            continue
        source_id = recommendation_pair_candidate(object_id, candidate_set)
        if source_id is None or source_id in paired:
            continue
        recommendations.append(
            {
                "kind": "pair",
                "object_id": object_id,
                "source_id": source_id,
                "mode": core.MODE_MIRROR_STRUCTURAL,
                "reason": "left/right name pair",
            }
        )
        paired.add(object_id)
        paired.add(source_id)

    for object_id in available:
        if object_id in paired:
            continue
        text = recommendation_text(context, object_id)
        if recommendation_matches(text, RECOMMEND_TRANSLATE_PATTERNS) and not recommendation_matches(
            text,
            RECOMMEND_TRANSLATE_EXCLUDE_PATTERNS,
        ):
            recommendations.append(
                {
                    "kind": "single",
                    "object_id": object_id,
                    "source_id": "",
                    "mode": core.MODE_TRANSLATE,
                    "reason": "driver control or instrument name",
                }
            )
        elif recommendation_matches(text, RECOMMEND_MIRROR_PATTERNS):
            recommendations.append(
                {
                    "kind": "single",
                    "object_id": object_id,
                    "source_id": "",
                    "mode": core.MODE_MIRROR,
                    "reason": "asymmetric interior name",
                }
            )

    mode_order = {
        core.MODE_TRANSLATE: 0,
        core.MODE_MIRROR: 1,
        core.MODE_MIRROR_STRUCTURAL: 2,
    }
    recommendations.sort(
        key=lambda item: (
            mode_order.get(item["mode"], 99),
            item["object_id"].lower(),
            item.get("source_id", "").lower(),
        )
    )
    return recommendations


def offset_label(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        return fmt_float(abs(float(value)))
    except (TypeError, ValueError):
        return ""


def offset_display(mode: str, value: object, *, manual_delta: bool) -> str:
    if mode != core.MODE_TRANSLATE:
        return "N/A"
    explicit = offset_label(value)
    if explicit:
        return explicit
    return "Manual" if manual_delta else "Auto"


def existing_initial_dir(path: object, fallback: Path) -> str:
    candidate = Path(str(path)) if path else fallback
    if candidate.is_file():
        candidate = candidate.parent
    if candidate.exists():
        return str(candidate)
    return str(fallback)


def app_icon_path() -> Path | None:
    for candidate in (RESOURCE_DIR / APP_ICON_NAME, THIS_DIR / APP_ICON_NAME):
        if candidate.exists():
            return candidate
    return None


class HandDriveToolApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BeamHDC Hand-Drive Conversion Tool")
        self._set_app_icon()
        self.geometry("1480x840")
        self.minsize(1160, 700)

        self.context: core.VehicleContext | None = None
        self.conversion: dict[str, object] = {}
        self.source_zip: Path | None = None
        self.vehicle_ids: list[str] = []
        # Model dropdown history: combo label -> (zip path, vehicle id)
        self.model_entries: dict[str, tuple[Path, str]] = {}
        self.model_load_busy = False
        self.settings = core.load_app_settings()
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_running = False
        self.part_resolver = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rhd-parts")
        self.variant_detector = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rhd-variants")
        self.variant_detection_seq = 0
        self.variant_detection_running = False
        self.variant_detection_pending = False
        self.variant_detected_hands: dict[str, str] = {}
        self.variant_detection_complete = False
        self.part_refresh_after_id: str | None = None
        self.part_refresh_running = False
        self.part_refresh_pending = False
        self.part_refresh_pending_reset = False
        self.part_refresh_seq = 0
        self.resolved_part_ids: list[str] = []
        self.vehicle_load_seq = 0
        self.recommendation_seq = 0
        self.recommendation_modal: tk.Toplevel | None = None
        self.recommendation_tree: ttk.Treeview | None = None
        self.recommendation_rows: dict[str, dict[str, str]] = {}
        self.structural_prompt_after_id: str | None = None
        self.structural_prompt_part_id: str | None = None
        self.structural_prompt_previous_mode: str = core.MODE_SKIP
        self.structural_prompt_open = False
        # Per-table click-to-sort state: tree -> (column id or None, descending)
        self._tree_sort: dict[ttk.Treeview, tuple[str | None, bool]] = {}
        self._tree_heading_text: dict[ttk.Treeview, dict[str, str]] = {}
        self.part_filter_entry: ttk.Entry | None = None

        self.source_var = tk.StringVar(value="No source zip loaded")
        self.vehicle_var = tk.StringVar()
        self.project_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar()
        self.auto_delta_var = tk.StringVar(value="")
        self.manual_delta_enabled = tk.BooleanVar(value=False)
        self.manual_delta_var = tk.StringVar(value="")
        self.mods_folder_var = tk.StringVar(value=str(self.settings.get("modsFolder") or ""))
        self.blender_var = tk.StringVar(value=str(self.settings.get("blenderExecutable") or ""))
        self.preview_output_var = tk.StringVar(value="")
        self.preview_output_to_config: dict[str, str] = {}
        # While the Preview output dropdown list is open, the highlighted (not
        # yet confirmed) entry hot-loads into the preview via this override.
        self.preview_output_hover: str | None = None
        self._preview_popdown_listbox: str | None = None
        self._preview_hover_after: str | None = None

        self.viewer: ModelPreview | None = None
        self.viewer_supports_scene = False
        self.mesh_scene_seq = 0
        self.mesh_scene_after: str | None = None
        self.mesh_scene_running = False
        self.mesh_scene_pending = False
        self.mesh_scene_hash: str | None = None
        self.mesh_scene_reset_pending = True
        self.current_part_ids: list[str] = []

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_theme()
        self._build_ui()
        self.bind("<KeyPress-h>", self._toggle_selected_parts_visibility_shortcut)
        self.bind("<KeyPress-H>", self._toggle_selected_parts_visibility_shortcut)
        self.bind("<KeyPress-space>", self._cycle_selected_part_mode_shortcut)
        self.bind("<Shift-KeyPress-space>", lambda event: self._cycle_selected_part_mode_shortcut(event, -1))
        self.bind_all("<Button-1>", self._clear_part_filter_focus_on_click, add="+")
        self._rebuild_model_combo()
        self.after_idle(self._maximize_on_start)
        self.after(120, self._poll_worker_queue)

    def _on_close(self) -> None:
        self._cancel_structural_prompt()
        self.part_resolver.shutdown(wait=False, cancel_futures=True)
        self.variant_detector.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    def _set_app_icon(self) -> None:
        icon_path = app_icon_path()
        if icon_path is None:
            return
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=str(icon_path))
            else:
                self.iconbitmap(str(icon_path))
        except tk.TclError:
            pass

    @staticmethod
    def _is_widget_or_child(widget: tk.Widget, parent: tk.Widget) -> bool:
        widget_path = str(widget)
        parent_path = str(parent)
        return widget_path == parent_path or widget_path.startswith(parent_path + ".")

    def _clear_part_filter_focus_on_click(self, event: tk.Event) -> None:
        filter_entry = self.part_filter_entry
        if filter_entry is None:
            return
        try:
            if self.focus_get() is not filter_entry:
                return
        except tk.TclError:
            return
        clicked = event.widget
        if clicked is not None and self._is_widget_or_child(clicked, filter_entry):
            return
        try:
            clicked.focus_set()
        except Exception:
            self.focus_set()

    def _part_display_name(self, object_id: str) -> str:
        if self.context is None:
            return object_id
        obj = self.context.objects.get(object_id)
        if obj is not None and not obj.dae_path and obj.name and obj.name != object_id:
            return f"{obj.name} [{object_id}]"
        prefix = f"{self.context.vehicle_id}_"
        if object_id.startswith(prefix):
            return object_id[len(prefix) :]
        return object_id

    def _configure_theme(self) -> None:
        self.ttk_style = ttk.Style(self)
        for theme in ("clam", "alt", "default"):
            if theme in self.ttk_style.theme_names():
                self.ttk_style.theme_use(theme)
                return

    def _maximize_on_start(self) -> None:
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def _current_monitor_work_area(self) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class RECT(ctypes.Structure):
                    _fields_ = (
                        ("left", wintypes.LONG),
                        ("top", wintypes.LONG),
                        ("right", wintypes.LONG),
                        ("bottom", wintypes.LONG),
                    )

                class MONITORINFO(ctypes.Structure):
                    _fields_ = (
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD),
                    )

                monitor = ctypes.windll.user32.MonitorFromWindow(self.winfo_id(), 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return work.left, work.top, work.right, work.bottom
            except Exception:
                pass
        left = self.winfo_vrootx()
        top = self.winfo_vrooty()
        return left, top, left + self.winfo_vrootwidth(), top + self.winfo_vrootheight()

    def _place_modal_on_app_monitor(self, modal: tk.Toplevel) -> None:
        self.update_idletasks()
        modal.update_idletasks()

        width = modal.winfo_width()
        height = modal.winfo_height()
        if width <= 1:
            width = modal.winfo_reqwidth()
        if height <= 1:
            height = modal.winfo_reqheight()

        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_w = max(self.winfo_width(), 1)
        parent_h = max(self.winfo_height(), 1)
        x = parent_x + (parent_w - width) // 2
        y = parent_y + (parent_h - height) // 2

        work_left, work_top, work_right, work_bottom = self._current_monitor_work_area()
        x = min(max(x, work_left), max(work_left, work_right - width))
        y = min(max(y, work_top), max(work_top, work_bottom - height))
        modal.geometry(f"{width}x{height}+{x}+{y}")

    def _show_error(self, title: str, message: str, *, parent: tk.Widget | None = None) -> None:
        messagebox.showerror(title, message, parent=parent or self)

    def _ask_open_filename(self, **options) -> str:
        return filedialog.askopenfilename(parent=self, **options)

    def _ask_directory(self, **options) -> str:
        return filedialog.askdirectory(parent=self, **options)

    def _configure_tree_rows(self, tree: ttk.Treeview) -> None:
        tree.tag_configure("evenrow", background="#ffffff")
        tree.tag_configure("oddrow", background="#cccccc")

    def _row_tags(self, index: int) -> tuple[str, ...]:
        return ("oddrow",) if index % 2 else ("evenrow",)

    # ----- generic click-to-sort for all table views -----------------------

    def _tree_column_name(self, tree: ttk.Treeview, column_id: str) -> str | None:
        """Map a display column id ('#3') to its logical column name so click
        handlers stay correct no matter how many columns a table has. Returns
        None for the tree column ('#0') or on any mismatch."""
        if not column_id or column_id == "#0":
            return None
        try:
            index = int(column_id[1:]) - 1
        except ValueError:
            return None
        columns = tree["columns"]
        if 0 <= index < len(columns):
            return str(columns[index])
        return None

    def _register_tree_headings(self, tree: ttk.Treeview, headings: dict[str, str]) -> None:
        """Record each heading's plain label and wire its heading button to sort
        the table by that column. `headings` maps a column id ('#0' or a column
        name) to its display label."""
        self._tree_heading_text[tree] = dict(headings)
        for column in headings:
            tree.heading(column, command=lambda c=column, t=tree: self._sort_tree(t, c))

    def _sort_tree(self, tree: ttk.Treeview, column: str) -> None:
        prev_column, prev_descending = self._tree_sort.get(tree, (None, False))
        descending = column == prev_column and not prev_descending
        self._tree_sort[tree] = (column, descending)
        self._apply_tree_sort(tree)

    @staticmethod
    def _sort_key(value: object) -> tuple[int, object]:
        # Numeric-parseable cells sort numerically ahead of text cells, so
        # coordinate/offset columns order by value while Y/N and text columns
        # order alphabetically -- and float is never compared against str.
        text = str(value).strip()
        try:
            return (0, float(text))
        except ValueError:
            return (1, text.lower())

    def _apply_tree_sort(self, tree: ttk.Treeview) -> None:
        """Reorder the rows in place per the tree's current sort selection.
        Row iids are preserved (only their visual order changes) so selection,
        preview picking, and part/config identity mapping are unaffected."""
        entry = self._tree_sort.get(tree)
        if not entry or entry[0] is None:
            return
        column, descending = entry
        children = list(tree.get_children(""))
        if not children:
            return
        if column == "#0":
            cell = lambda iid: tree.item(iid, "text")
        else:
            cell = lambda iid: tree.set(iid, column)
        ordered = sorted(children, key=lambda iid: self._sort_key(cell(iid)), reverse=descending)
        for index, iid in enumerate(ordered):
            tree.move(iid, "", index)
            tree.item(iid, tags=self._row_tags(index))
        self._update_sort_indicators(tree)

    def _restore_tree_order(self, tree: ttk.Treeview, previous_order: list[str]) -> None:
        children = list(tree.get_children(""))
        if not children:
            return
        existing = set(children)
        seen: set[str] = set()
        ordered: list[str] = []
        for iid in previous_order:
            if iid in existing and iid not in seen:
                ordered.append(iid)
                seen.add(iid)
        ordered.extend(iid for iid in children if iid not in seen)
        for index, iid in enumerate(ordered):
            tree.move(iid, "", index)
            tree.item(iid, tags=self._row_tags(index))

    @staticmethod
    def _tree_body_click(tree: ttk.Treeview, event: tk.Event) -> bool:
        return tree.identify_region(event.x, event.y) in {"tree", "cell"}

    def _update_sort_indicators(self, tree: ttk.Treeview) -> None:
        base = self._tree_heading_text.get(tree)
        if not base:
            return
        entry = self._tree_sort.get(tree)
        sort_column, descending = entry if entry else (None, False)
        arrow = " ▼" if descending else " ▲"
        for column, label in base.items():
            tree.heading(column, text=label + arrow if column == sort_column else label)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(2, weight=1)
        top.columnconfigure(5, weight=0)

        self.open_button = ttk.Button(top, text="Open Vehicle Zip", command=self._open_zip_dialog)
        self.open_button.grid(row=0, column=0, sticky="w")
        self.refresh_button = ttk.Button(
            top,
            text="Refresh",
            command=lambda: self._load_selected_vehicle(force_reload=True),
            state="disabled",
        )
        self.refresh_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(top, textvariable=self.source_var).grid(row=0, column=2, sticky="ew", padx=(8, 16))
        ttk.Label(top, text="Model").grid(row=0, column=3, sticky="e")
        self.vehicle_combo = ttk.Combobox(top, textvariable=self.vehicle_var, state="disabled", width=22)
        self.vehicle_combo.grid(row=0, column=4, sticky="w", padx=(6, 12))
        self.vehicle_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_model_selected())
        ttk.Button(top, text="Save Config", command=self._save_config).grid(row=0, column=5, sticky="e")
        ttk.Button(top, text="Import Config", command=self._import_config_dialog).grid(row=0, column=6, sticky="e", padx=(6, 0))

        ttk.Label(top, textvariable=self.project_var).grid(row=1, column=0, columnspan=7, sticky="ew", pady=(6, 0))

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)

        left = ttk.Frame(main)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=0)
        left.rowconfigure(3, weight=1)
        main.add(left, weight=1)

        right = ttk.Frame(main)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        main.add(right, weight=10)

        self._build_variant_panel(left)
        self._build_part_panel(left)
        self._build_right_panel(right)

        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.detail_var).grid(row=0, column=0, sticky="w")
        ttk.Label(bottom, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

    def _build_variant_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(header, text="Variants").pack(side="left")
        ttk.Button(header, text="Clear Used", command=lambda: self._set_all_variants_selected(False)).pack(
            side="right"
        )
        ttk.Button(header, text="Use All", command=lambda: self._set_all_variants_selected(True)).pack(
            side="right",
            padx=(0, 6),
        )

        frame = ttk.Frame(parent)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = ("selected", "config", "display", "detected", "override", "output")
        self.variant_tree = ttk.Treeview(frame, columns=columns, show="headings", height=8, selectmode="browse")
        headings = {
            "selected": "Use",
            "config": "Config",
            "display": "Display Name",
            "detected": "Detected",
            "override": "Override",
            "output": "Output",
        }
        widths = {
            "selected": 54,
            "config": 130,
            "display": 260,
            "detected": 80,
            "override": 92,
            "output": 160,
        }
        for col in columns:
            self.variant_tree.heading(
                col,
                text=headings[col],
                anchor="w",
            )
            self.variant_tree.column(
                col,
                width=widths[col],
                minwidth=48,
                stretch=col in {"display", "output"},
                anchor="center" if col == "selected" else "w",
            )
        self._register_tree_headings(self.variant_tree, headings)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.variant_tree.yview)
        self.variant_tree.configure(yscrollcommand=yscroll.set)
        self.variant_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self._configure_tree_rows(self.variant_tree)
        self.variant_tree.bind("<Button-1>", self._variant_click)
        self.variant_tree.bind("<Double-1>", self._variant_double_click)

    def _build_part_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=2, column=0, sticky="ew", pady=(10, 4))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Parts Used by Selected Variants").grid(row=0, column=0, sticky="w")
        self.part_filter_entry = ttk.Entry(header, textvariable=self.filter_var)
        self.part_filter_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.part_filter_entry.insert(0, "")
        self.recommend_button = ttk.Button(
            header,
            text="Recommend Modes",
            command=self._open_recommendations_modal,
            state="disabled",
        )
        self.recommend_button.grid(row=0, column=2, sticky="e")
        self.show_all_parts_button = ttk.Button(
            header,
            text="Show All",
            command=lambda: self._set_all_parts_visible(True),
            state="disabled",
        )
        self.show_all_parts_button.grid(row=0, column=3, sticky="e", padx=(6, 0))
        self.hide_all_parts_button = ttk.Button(
            header,
            text="Hide All",
            command=lambda: self._set_all_parts_visible(False),
            state="disabled",
        )
        self.hide_all_parts_button.grid(row=0, column=4, sticky="e", padx=(6, 0))
        self.filter_var.trace_add("write", lambda *_args: self._refresh_parts())

        frame = ttk.Frame(parent)
        frame.grid(row=3, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("visible", "solo", "active", "mode", "offset", "steering", "x", "y", "z")
        self.part_tree = ttk.Treeview(frame, columns=columns, show=("tree", "headings"), selectmode="extended")
        self.part_tree.heading("#0", text="Part", anchor="w")
        self.part_tree.column("#0", width=250, minwidth=150, stretch=True, anchor="w")
        headings = {
            "mode": "Mode",
            "offset": "Offset X",
            "steering": "Steering Ref",
            "visible": "Visible",
            "solo": "Solo",
            "active": "Active",
            "x": "X",
            "y": "Y",
            "z": "Z",
        }
        widths = {
            "mode": 132,
            "offset": 82,
            "steering": 96,
            "visible": 70,
            "solo": 60,
            "active": 64,
            "x": 82,
            "y": 82,
            "z": 82,
        }
        for col in columns:
            self.part_tree.heading(
                col,
                text=headings[col],
                anchor="w",
            )
            self.part_tree.column(
                col,
                width=widths[col],
                minwidth=50,
                stretch=False,
                anchor="center" if col in {"steering", "visible", "solo", "active"} else "w",
            )
        self._register_tree_headings(self.part_tree, {"#0": "Part", **headings})
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.part_tree.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.part_tree.xview)
        self.part_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.part_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self._configure_tree_rows(self.part_tree)
        self.part_tree.bind("<<TreeviewSelect>>", lambda _event: self._part_selection_changed())
        self.part_tree.bind("<Button-1>", self._part_click)
        self.part_tree.bind("<Button-3>", self._part_right_click)
        self.part_tree.bind("<Motion>", self._part_motion)
        self.part_tree.bind("<Leave>", self._part_leave)
        self.part_tree.bind("<Double-1>", self._part_double_click)
        self.part_tree.bind("<KeyPress-space>", self._cycle_selected_part_mode_shortcut)
        self.part_tree.bind("<Shift-KeyPress-space>", lambda event: self._cycle_selected_part_mode_shortcut(event, -1))

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        self.viewer_holder = ttk.Frame(parent)
        self.viewer_holder.grid(row=0, column=0, sticky="nsew")
        self.viewer_holder.columnconfigure(0, weight=1)
        self.viewer_holder.rowconfigure(0, weight=1)
        ttk.Label(self.viewer_holder, text="Load a vehicle zip to use the built-in part viewer").grid(row=0, column=0)

        controls = ttk.LabelFrame(parent, text="Build Settings", padding=8)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Auto delta X").grid(row=0, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.auto_delta_var).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Checkbutton(
            controls,
            text="Manual magnitude",
            variable=self.manual_delta_enabled,
            command=self._manual_delta_toggled,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.manual_delta_entry = ttk.Entry(controls, textvariable=self.manual_delta_var, width=12)
        self.manual_delta_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.manual_delta_entry.bind("<FocusOut>", lambda _event: self._commit_delta_from_ui())
        self.manual_delta_entry.bind("<Return>", lambda _event: self._commit_delta_from_ui())

        ttk.Label(controls, text="Mods folder").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.mods_folder_var).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Button(controls, text="Browse", command=self._browse_mods_folder).grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(10, 0))

        ttk.Label(controls, text="Blender exe").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(controls, textvariable=self.blender_var).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        ttk.Button(controls, text="Browse", command=self._browse_blender).grid(row=3, column=2, sticky="e", padx=(6, 0), pady=(6, 0))

        ttk.Label(controls, text="Preview output").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.preview_output_combo = ttk.Combobox(
            controls,
            textvariable=self.preview_output_var,
            state="disabled",
            width=28,
        )
        self.preview_output_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(6, 0))
        self.preview_output_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._preview_output_selected(),
        )
        self._wire_preview_output_popdown()

        buttons = ttk.Frame(parent)
        buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        buttons.columnconfigure((0, 1), weight=1)
        self.install_button = ttk.Button(buttons, text="Build + Install", command=lambda: self._start_build(install=True))
        self.install_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.blender_button = ttk.Button(buttons, text="Blender Preview", command=self._start_blender_preview)
        self.blender_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _open_zip_dialog(self) -> None:
        initial = existing_initial_dir(self.settings.get("lastVehicleZipFolder"), core.WORKSPACE_DIR)
        path = self._ask_open_filename(
            title="Open BeamNG vehicle zip",
            initialdir=initial,
            filetypes=(("Zip files", "*.zip"), ("All files", "*.*")),
        )
        if path:
            self._load_source_zip(Path(path))

    # ----- Model dropdown history -----------------------------------------

    def _recent_vehicle_entries(self) -> list[tuple[Path, str]]:
        """Persisted (zip, vehicle id) history, newest first, malformed rows
        dropped. Missing zips are kept so the history survives an unplugged
        drive; they are handled when actually selected."""
        recent = self.settings.get("recentVehicles")
        if not isinstance(recent, list):
            return []
        entries: list[tuple[Path, str]] = []
        for item in recent:
            if not isinstance(item, dict):
                continue
            zip_str = str(item.get("zip") or "")
            vehicle_id = str(item.get("vehicleId") or "")
            if zip_str and vehicle_id:
                entries.append((Path(zip_str), vehicle_id))
        return entries

    def _record_recent_vehicle(self, source_zip: Path, vehicle_id: str) -> None:
        zip_str = str(source_zip)
        recent = self.settings.get("recentVehicles")
        if not isinstance(recent, list):
            recent = []
        deduped = [
            item
            for item in recent
            if isinstance(item, dict)
            and not (str(item.get("zip")) == zip_str and str(item.get("vehicleId")) == vehicle_id)
        ]
        deduped.insert(0, {"zip": zip_str, "vehicleId": vehicle_id})
        self.settings["recentVehicles"] = deduped[:MODEL_HISTORY_LIMIT]

    def _prune_recent_vehicle(self, source_zip: Path, vehicle_id: str) -> None:
        zip_str = str(source_zip)
        recent = self.settings.get("recentVehicles")
        if not isinstance(recent, list):
            return
        self.settings["recentVehicles"] = [
            item
            for item in recent
            if isinstance(item, dict)
            and not (str(item.get("zip")) == zip_str and str(item.get("vehicleId")) == vehicle_id)
        ]
        core.save_app_settings(self.settings)

    @staticmethod
    def _model_history_label(zip_path: Path, vehicle_id: str, taken: dict[str, object]) -> str:
        label = f"{vehicle_id}  ({zip_path.stem})"
        base = label
        suffix = 2
        while label in taken:
            label = f"{base} #{suffix}"
            suffix += 1
        return label

    def _rebuild_model_combo(self) -> None:
        """Rebuild the Model dropdown to hold the currently-open zip's vehicles
        plus recently-opened (zip, vehicle) combos, and remember which load each
        label maps to. Current-zip vehicles keep bare vehicle-id labels so the
        existing load path (which reads the id straight off the combo) is
        unchanged; cross-zip history entries are labelled with the zip stem."""
        entries: dict[str, tuple[Path, str]] = {}
        values: list[str] = []
        current_zip = str(self.source_zip) if self.source_zip is not None else None
        if self.source_zip is not None:
            for vid in self.vehicle_ids:
                if vid in entries:
                    continue
                entries[vid] = (self.source_zip, vid)
                values.append(vid)
        for zip_path, vid in self._recent_vehicle_entries():
            if current_zip is not None and str(zip_path) == current_zip and vid in self.vehicle_ids:
                continue  # already represented by the open zip's bare label
            label = self._model_history_label(zip_path, vid, entries)
            entries[label] = (zip_path, vid)
            values.append(label)
        self.model_entries = entries
        self.vehicle_combo.configure(values=values)
        self._update_model_combo_state()

    def _update_model_combo_state(self) -> None:
        count = len(self.vehicle_combo.cget("values"))
        if self.model_load_busy or count < 2:
            self.vehicle_combo.configure(state="disabled")
        else:
            self.vehicle_combo.configure(state="readonly")

    def _on_model_selected(self) -> None:
        label = self.vehicle_var.get()
        entry = self.model_entries.get(label)
        if entry is None:
            # Bare vehicle id from the open zip (older/direct path).
            self._load_selected_vehicle()
            return
        zip_path, vehicle_id = entry
        if self.source_zip is not None and str(zip_path) == str(self.source_zip):
            self.vehicle_var.set(vehicle_id)  # bare label for the load path
            self._load_selected_vehicle()
            return
        if not zip_path.exists():
            self._show_error(
                "Vehicle unavailable",
                f"This zip no longer exists and was removed from history:\n{zip_path}",
            )
            self._prune_recent_vehicle(zip_path, vehicle_id)
            # Restore the dropdown to the loaded vehicle and refresh the list.
            if self.context is not None:
                self.vehicle_var.set(self.context.vehicle_id)
            self._rebuild_model_combo()
            return
        self._load_source_zip(zip_path, vehicle_id)

    def _load_source_zip(self, source_zip: Path, vehicle_id: str | None = None) -> None:
        try:
            vehicle_ids = core.vehicle_ids_in_zip(source_zip)
            if not vehicle_ids:
                raise RuntimeError("No vehicles/<model>/ content with DAE/PC/JBeam files was found")
            self.source_zip = source_zip
            self.settings["lastVehicleZipFolder"] = str(source_zip.parent)
            core.save_app_settings(self.settings)
            self.vehicle_ids = vehicle_ids
            self.source_var.set(str(source_zip))
            selected_vehicle = vehicle_id if vehicle_id in vehicle_ids else vehicle_ids[0]
            self.vehicle_var.set(selected_vehicle)
            self._rebuild_model_combo()
            self._load_selected_vehicle()
        except Exception as exc:
            self._show_error("Open zip failed", str(exc))
            self.status_var.set("Open zip failed")

    def _load_selected_vehicle(self, *, force_reload: bool = False) -> None:
        if self.source_zip is None:
            return
        self._cancel_structural_prompt()
        vehicle_id = self.vehicle_var.get() or (self.vehicle_ids[0] if self.vehicle_ids else None)
        if not vehicle_id:
            return
        self.vehicle_load_seq += 1
        seq = self.vehicle_load_seq
        if force_reload:
            self.status_var.set(f"Re-scanning vehicles/{vehicle_id} (ignoring cache)...")
        else:
            self.status_var.set(f"Loading vehicles/{vehicle_id}...")
        self._set_load_busy(True)
        worker = threading.Thread(
            target=self._vehicle_load_worker,
            args=(self.source_zip, vehicle_id, force_reload, seq),
            daemon=True,
        )
        worker.start()

    def _vehicle_load_worker(
        self,
        source_zip: Path,
        vehicle_id: str,
        force_reload: bool,
        seq: int,
    ) -> None:
        try:
            context = core.load_vehicle_context(source_zip, vehicle_id, use_cache=not force_reload)
            if force_reload:
                core.clear_parts_cache(context)
            conversion, loaded = core.load_or_create_conversion(context)
            self.worker_queue.put(
                ("vehicle_load_success", (seq, source_zip, vehicle_id, context, conversion, loaded))
            )
        except Exception as exc:
            self.worker_queue.put(("vehicle_load_error", (seq, exc)))

    def _set_load_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.open_button.configure(state=state)
        self.refresh_button.configure(state="disabled" if busy or self.context is None else "normal")
        self.recommend_button.configure(state="disabled" if busy or self.context is None else "normal")
        self.show_all_parts_button.configure(state="disabled" if busy or self.context is None else "normal")
        self.hide_all_parts_button.configure(state="disabled" if busy or self.context is None else "normal")
        self.model_load_busy = busy
        self._update_model_combo_state()
        self._set_busy(busy)

    def _handle_vehicle_load_success(self, payload: object) -> None:
        seq, source_zip, vehicle_id, context, conversion, loaded = payload
        if seq != self.vehicle_load_seq:
            return
        self.context = context
        self.conversion = conversion
        self.preview_output_var.set("")
        self.variant_detected_hands = {}
        self.variant_detection_complete = False
        self.variant_detection_pending = False
        self.settings["lastVehicleZipPath"] = str(source_zip)
        self.settings["lastVehicleId"] = vehicle_id
        self._record_recent_vehicle(source_zip, vehicle_id)
        core.save_app_settings(self.settings)
        self.vehicle_var.set(vehicle_id)
        self._rebuild_model_combo()
        self.part_refresh_seq += 1
        self.resolved_part_ids = []
        self.current_part_ids = []
        self.mesh_scene_hash = None
        self.mesh_scene_reset_pending = True
        self._set_load_busy(False)
        self._sync_delta_to_ui()
        self._replace_viewer()
        self._refresh_all(reset_view=True)
        self._schedule_variant_detection()
        self._schedule_mesh_scene(immediate=True)
        loaded_text = "loaded exact project config" if loaded else "new project config"
        self.project_var.set(f"Project: {context.project_dir} ({loaded_text})")
        from_cache = " (from cache)" if getattr(context, "loaded_from_cache", False) else ""
        self.status_var.set(
            f"Loaded {context.vehicle_id}{from_cache}: {len(context.variants)} variant(s), "
            f"{len(context.objects)} DAE object(s)"
        )

    def _handle_vehicle_load_error(self, payload: object) -> None:
        seq, exc = payload
        if seq != self.vehicle_load_seq:
            return
        self._set_load_busy(False)
        self._show_error("Load vehicle failed", str(exc))
        self.status_var.set("Load vehicle failed")

    def _replace_viewer(self) -> None:
        if self.viewer is not None and self.viewer_supports_scene:
            try:
                self.viewer.destroy()  # releases the GL context
            except Exception:
                pass
        for child in self.viewer_holder.winfo_children():
            child.destroy()
        self.viewer = None
        self.viewer_supports_scene = False
        if self.context is None:
            return
        if mesh_preview is not None:
            try:
                self.viewer = mesh_preview.MeshPreview(self.viewer_holder)
                self.viewer_supports_scene = True
                self.viewer.on_pick = self._on_preview_pick
                self.viewer.set_message("building preview...")
            except Exception as exc:
                print(f"[preview] GPU mesh preview unavailable ({exc}); using box preview")
                self.viewer = None
        if self.viewer is None:
            self.viewer = ModelPreview(self.viewer_holder, self.context.preview_by_id)
        self.viewer.grid(row=0, column=0, sticky="nsew")

    def _sync_delta_to_ui(self) -> None:
        delta = self.conversion.get("delta", {})
        if not isinstance(delta, dict):
            delta = {}
            self.conversion["delta"] = delta
        self.manual_delta_enabled.set(bool(delta.get("manual")))
        magnitude = delta.get("magnitude")
        self.manual_delta_var.set("" if magnitude in (None, "") else fmt_float(abs(float(magnitude))))
        self._manual_delta_toggled(refresh=False)

    def _refresh_all(self, *, reset_view: bool = False) -> None:
        self._refresh_variants()
        self._schedule_parts_refresh(reset_view=reset_view)
        self._refresh_delta_label()
        self._update_detail()

    def _refresh_variants(self) -> None:
        if self.context is None:
            return
        keep = set(self.variant_tree.selection())
        previous_order = list(self.variant_tree.get_children(""))
        for item in self.variant_tree.get_children():
            self.variant_tree.delete(item)
        variants = self.conversion.setdefault("variants", {})
        row_index = 0
        for config_name, variant in sorted(self.context.variants.items()):
            settings = variants.setdefault(
                config_name,
                {
                    "selected": False,
                    "sourceHandOverride": core.HAND_AUTO,
                },
            )
            if not isinstance(settings, dict):
                continue
            detected = self._detected_hand_for_ui(config_name)
            override = str(settings.get("sourceHandOverride", core.HAND_AUTO))
            output = self._variant_output_name_for_ui(config_name, settings, detected)
            self.variant_tree.insert(
                "",
                "end",
                iid=config_name,
                tags=self._row_tags(row_index),
                values=(
                    yn_label(settings.get("selected")),
                    config_name,
                    variant.display_name,
                    detected,
                    override,
                    output,
                ),
            )
            row_index += 1
        self._restore_tree_order(self.variant_tree, previous_order)
        visible_keep = [item for item in keep if self.variant_tree.exists(item)]
        if visible_keep:
            self.variant_tree.selection_set(visible_keep)
        self._refresh_preview_outputs()

    def _detected_hand_for_ui(self, config_name: str) -> str:
        return self.variant_detected_hands.get(config_name, "..." if not self.variant_detection_complete else core.HAND_UNKNOWN)

    def _variant_output_name_for_ui(
        self,
        config_name: str,
        settings: dict[str, object],
        detected: str | None = None,
    ) -> str:
        detected = detected if detected is not None else self._detected_hand_for_ui(config_name)
        override = str(settings.get("sourceHandOverride", core.HAND_AUTO))
        source = override if override != core.HAND_AUTO else detected
        if source == "...":
            return "detecting"
        target = core.target_hand_for(source, core.ACTION_OPPOSITE)
        return "skip" if target is None else core.variant_output_name(config_name, target)

    def _output_config_sources_for_ui(self) -> dict[str, str]:
        if self.context is None:
            return {}
        variants = self.conversion.get("variants", {})
        if not isinstance(variants, dict):
            return {}
        choices: dict[str, str] = {}
        for config_name, settings in variants.items():
            if config_name not in self.context.variants or not isinstance(settings, dict):
                continue
            if not settings.get("selected"):
                continue
            detected = self._detected_hand_for_ui(config_name)
            output = self._variant_output_name_for_ui(config_name, settings, detected)
            if output not in {"skip", "detecting"}:
                choices[output] = config_name
        return choices

    def _refresh_preview_outputs(self) -> None:
        if self.context is None or not hasattr(self, "preview_output_combo"):
            self.preview_output_to_config = {}
            self.preview_output_var.set("")
            return
        current = self.preview_output_var.get()
        choices = self._output_config_sources_for_ui()
        self.preview_output_to_config = choices
        values = sorted(choices)
        self.preview_output_combo.configure(values=values)
        if current in choices:
            selected = current
        else:
            selected = self._cached_preview_output(choices)
            tree_selection = self.variant_tree.selection()
            if tree_selection:
                output = str(self.variant_tree.set(tree_selection[0], "output"))
                if not selected and output in choices:
                    selected = output
            if not selected and values:
                selected = values[0]
        self.preview_output_var.set(selected)
        if self.worker_running or not values:
            self.preview_output_combo.configure(state="disabled")
        else:
            self.preview_output_combo.configure(state="readonly")

    def _preview_output_cache_key(self) -> str | None:
        if self.context is None:
            return None
        source = str(self.context.source_zip.resolve(strict=False))
        return f"{source}|{self.context.vehicle_id}"

    def _cached_preview_output(self, choices: dict[str, str]) -> str:
        key = self._preview_output_cache_key()
        cache = self.settings.setdefault("previewOutputByVehicle", {})
        if not key or not isinstance(cache, dict):
            return ""
        entry = cache.get(key)
        if isinstance(entry, dict):
            output = str(entry.get("output") or "")
            if output in choices:
                return output
            config = str(entry.get("config") or "")
            if config:
                for label, source_config in choices.items():
                    if source_config == config:
                        return label
        elif isinstance(entry, str) and entry in choices:
            return entry
        return ""

    def _remember_preview_output(self, label: str | None = None) -> None:
        key = self._preview_output_cache_key()
        if key is None:
            return
        output = (label if label is not None else self.preview_output_var.get()).strip()
        config = self.preview_output_to_config.get(output)
        if not output or not config:
            return
        cache = self.settings.setdefault("previewOutputByVehicle", {})
        if not isinstance(cache, dict):
            cache = {}
            self.settings["previewOutputByVehicle"] = cache
        cache[key] = {"output": output, "config": config}
        core.save_app_settings(self.settings)

    def _preview_output_selected(self) -> None:
        self.preview_output_hover = None
        self._remember_preview_output()
        self._schedule_mesh_scene(immediate=True)

    def _wire_preview_output_popdown(self) -> None:
        """Hot-load trims while scrolling the Preview output dropdown. The ttk
        combobox popdown listbox is a plain Tcl widget with no Python wrapper;
        watch it via its <Map> event and poll the highlighted entry while it
        stays open."""
        combo = self.preview_output_combo
        try:
            popdown = str(combo.tk.call("ttk::combobox::PopdownWindow", combo))
            listbox = f"{popdown}.f.l"
            if not int(combo.tk.call("winfo", "exists", listbox)):
                return
            start = combo.register(self._start_preview_hover_watch)
            combo.tk.call("bind", listbox, "<Map>", f"+{start}")
        except tk.TclError:
            return
        self._preview_popdown_listbox = listbox

    def _start_preview_hover_watch(self) -> None:
        if self._preview_hover_after is not None:
            try:
                self.after_cancel(self._preview_hover_after)
            except Exception:
                pass
            self._preview_hover_after = None
        self._preview_hover_poll()

    def _preview_hover_poll(self) -> None:
        self._preview_hover_after = None
        combo = self.preview_output_combo
        listbox = self._preview_popdown_listbox
        mapped = False
        label = None
        if listbox is not None:
            try:
                mapped = bool(int(combo.tk.call("winfo", "ismapped", listbox)))
                if mapped:
                    selection = combo.tk.call(listbox, "curselection")
                    if selection:
                        index = selection[0] if isinstance(selection, (tuple, list)) else selection
                        label = str(combo.tk.call(listbox, "get", index))
            except tk.TclError:
                mapped = False
        if not mapped:
            self._end_preview_hover_watch()
            return
        if (
            label
            and label in self.preview_output_to_config
            and label != (self.preview_output_hover or self.preview_output_var.get())
        ):
            self.preview_output_hover = label
            self._schedule_mesh_scene(immediate=True)
        self._preview_hover_after = self.after(90, self._preview_hover_poll)

    def _end_preview_hover_watch(self) -> None:
        if self.preview_output_hover is None:
            return
        self.preview_output_hover = None
        # Confirming fires <<ComboboxSelected>> with the same trim already
        # loaded (snapshot-guarded no-op); after a cancel this restores the
        # preview of the actual selection.
        self._schedule_mesh_scene(immediate=True)

    def _variant_detection_signature(self) -> tuple[str, ...]:
        return tuple(sorted(core.selected_steering_refs(self.conversion)))

    def _invalidate_variant_detection(self) -> None:
        self.variant_detected_hands = {}
        self.variant_detection_complete = False
        self.mesh_scene_hash = None

    def _schedule_variant_detection(self) -> None:
        if self.context is None:
            return
        if self.variant_detection_running:
            self.variant_detection_pending = True
            return
        self._start_variant_detection()

    def _start_variant_detection(self) -> None:
        if self.context is None:
            return
        self.variant_detection_running = True
        self.variant_detection_pending = False
        self.variant_detection_seq += 1
        seq = self.variant_detection_seq
        context = self.context
        signature = self._variant_detection_signature()
        conversion_copy = json.loads(json.dumps(self.conversion, default=str))
        future = self.variant_detector.submit(self._variant_detection_worker, context, conversion_copy)
        future.add_done_callback(
            lambda completed, current_seq=seq, current_context=context, current_signature=signature: self.worker_queue.put(
                ("variant_hands_done", (current_seq, current_context, current_signature, completed))
            )
        )

    @staticmethod
    def _variant_detection_worker(
        context: core.VehicleContext,
        conversion: dict[str, object],
    ) -> dict[str, str]:
        return {
            config_name: core.detect_hand_for_variant(context, conversion, config_name)
            for config_name in sorted(context.variants)
        }

    def _handle_variant_hands_done(self, payload: object) -> None:
        seq, context, signature, completed = payload
        self.variant_detection_running = False
        should_apply = (
            seq == self.variant_detection_seq
            and context is self.context
            and signature == self._variant_detection_signature()
        )
        try:
            detected = completed.result()
        except Exception as exc:
            if should_apply:
                self.variant_detected_hands = {}
                self.variant_detection_complete = True
                self._refresh_variants()
                self.status_var.set(f"Trim handedness detection failed: {exc}")
            if self.variant_detection_pending:
                self._schedule_variant_detection()
            return
        if should_apply:
            self.variant_detected_hands = {
                config_name: hand
                for config_name, hand in detected.items()
                if hand in {core.HAND_LHD, core.HAND_RHD, core.HAND_UNKNOWN}
            }
            self.variant_detection_complete = True
            self.mesh_scene_hash = None
            self._refresh_variants()
            self._schedule_mesh_scene(immediate=True)
        if self.variant_detection_pending:
            self._schedule_variant_detection()

    def _refresh_parts(self, *, reset_view: bool = False) -> None:
        if self.context is None:
            return
        query = self.filter_var.get().strip().lower()
        keep = set(self.part_tree.selection())
        previous_order = list(self.part_tree.get_children(""))
        for item in self.part_tree.get_children():
            self.part_tree.delete(item)

        parts = self.conversion.setdefault("parts", {})
        ids = self.resolved_part_ids
        active_ids = self._preview_active_ids()
        displayed: list[str] = []
        row_index = 0
        for object_id in ids:
            obj = self.context.objects.get(object_id)
            if obj is None:
                continue
            settings = parts.setdefault(
                object_id,
                {
                    "mode": core.MODE_SKIP,
                    "mirrorSource": None,
                    "translateOffset": None,
                    "steeringRef": False,
                    "viewerVisible": True,
                    "viewerSolo": False,
                },
            )
            if not isinstance(settings, dict):
                continue
            mode = str(settings.get("mode", core.MODE_SKIP))
            display_name = self._part_display_name(object_id)
            if (
                query
                and query not in object_id.lower()
                and query not in display_name.lower()
                and query not in mode
            ):
                continue
            displayed.append(object_id)
            self.part_tree.insert(
                "",
                "end",
                iid=object_id,
                text=display_name,
                tags=self._row_tags(row_index),
                values=(
                    yn_label(settings.get("viewerVisible", True)),
                    yn_label(settings.get("viewerSolo")),
                    yn_label(object_id in active_ids),
                    mode_label(mode),
                    offset_display(
                        mode,
                        settings.get("translateOffset"),
                        manual_delta=self.manual_delta_enabled.get(),
                    ),
                    yn_label(settings.get("steeringRef")),
                    fmt_float(obj.x),
                    fmt_float(obj.y),
                    fmt_float(obj.z),
                ),
            )
            row_index += 1
        self.current_part_ids = displayed
        self._restore_tree_order(self.part_tree, previous_order)
        visible_keep = [item for item in keep if self.part_tree.exists(item)]
        if visible_keep:
            self.part_tree.selection_set(visible_keep)
        self._refresh_viewer(reset=reset_view)

    def _schedule_parts_refresh(self, *, reset_view: bool = False) -> None:
        self.part_refresh_pending_reset = self.part_refresh_pending_reset or reset_view
        if self.part_refresh_running:
            self.part_refresh_pending = True
            self.part_refresh_seq += 1
            return
        if self.part_refresh_after_id is not None:
            return
        self.part_refresh_after_id = self.after_idle(self._run_scheduled_parts_refresh)

    def _run_scheduled_parts_refresh(self) -> None:
        self.part_refresh_after_id = None
        reset_view = self.part_refresh_pending_reset
        self.part_refresh_pending = False
        self.part_refresh_pending_reset = False
        self._start_parts_refresh(reset_view=reset_view)

    def _start_parts_refresh(self, *, reset_view: bool = False) -> None:
        self.part_refresh_after_id = None
        if self.context is None:
            self.resolved_part_ids = []
            self._refresh_parts(reset_view=reset_view)
            return
        selected = tuple(self._selected_variant_names())
        self.part_refresh_seq += 1
        seq = self.part_refresh_seq
        if not selected:
            self.resolved_part_ids = []
            self._refresh_parts(reset_view=reset_view)
            self.status_var.set("No trims selected; 0 used part(s) displayed")
            return
        context = self.context
        cached_ids = core.load_cached_part_ids(context, selected)
        if cached_ids is not None:
            self.resolved_part_ids = [part_id for part_id in cached_ids if part_id in context.objects]
            self._refresh_parts(reset_view=reset_view)
            self._update_detail()
            self.status_var.set(f"{len(self.current_part_ids)} used part(s) displayed (parts cache)")
            return
        self.status_var.set(f"Resolving used parts for {len(selected)} trim(s)...")
        self.part_refresh_running = True
        future = self.part_resolver.submit(self._resolve_part_ids_worker, context, selected)
        future.add_done_callback(
            lambda completed, current_seq=seq, current_context=context, should_reset=reset_view, current_selected=selected: self.worker_queue.put(
                ("parts_success", (current_seq, current_context, should_reset, current_selected, completed))
            )
        )

    @staticmethod
    def _resolve_part_ids_worker(
        context: core.VehicleContext,
        selected: tuple[str, ...],
    ) -> list[str]:
        _flex, _props, all_meshes = core.selected_mesh_roles(context, list(selected))
        return sorted(mesh for mesh in all_meshes if mesh in context.objects)

    def _selected_variant_names(self) -> list[str]:
        if self.context is None:
            return []
        variants = self.conversion.get("variants", {})
        if not isinstance(variants, dict):
            return []
        return [
            name
            for name, settings in variants.items()
            if name in self.context.variants and isinstance(settings, dict) and settings.get("selected")
        ]

    def _preview_base_part_ids(self) -> list[str]:
        if self.context is None:
            return []
        return [
            object_id
            for object_id in (self.resolved_part_ids or self.current_part_ids)
            if object_id in self.context.objects
        ]

    def _resolved_visible_ids(self) -> set[str]:
        """The set of parts actually present in the active preview / final
        visible output for the current variant selection: Solo (if any part is
        soloed) or per-part Visible toggles, over the resolved used-part set.
        Table selection deliberately has no effect here -- Visible/Solo have the
        final say over what the preview and the converted output contain."""
        if self.context is None:
            return set()
        parts = self.conversion.get("parts", {})
        base_ids = self._preview_base_part_ids()
        solo_ids = {
            object_id
            for object_id in base_ids
            if isinstance(parts, dict)
            and isinstance(parts.get(object_id), dict)
            and parts[object_id].get("viewerSolo")
        }
        if solo_ids:
            return solo_ids
        return {
            object_id
            for object_id in base_ids
            if not isinstance(parts, dict)
            or not isinstance(parts.get(object_id), dict)
            or parts[object_id].get("viewerVisible", True)
        }

    def _refresh_viewer(self, *, reset: bool = False) -> None:
        if self.viewer is None:
            return
        visible_ids = self._resolved_visible_ids()
        # Selected inactive parts are temporarily injected into the GPU scene
        # (scene.extra); show them while they stay selected. Intersecting with
        # the live selection hides a stale extra instantly after deselection,
        # before the scene rebuild that drops it has landed.
        scene = getattr(self.viewer, "scene", None)
        visible_ids |= set(getattr(scene, "extra", ()) or ()) & set(self.part_tree.selection())
        dimmed_ids = visible_ids - set(self.current_part_ids)
        self.viewer.set_visible_ids(list(visible_ids), reset=reset)
        if hasattr(self.viewer, "set_dimmed_ids"):
            self.viewer.set_dimmed_ids(dimmed_ids)
        # Selection only drives the highlight outline (skipped for hidden parts
        # in the renderer); it never adds a part to the visible set above.
        self.viewer.set_selected_ids(set(self.part_tree.selection()))

    def _preview_active_ids(self) -> set[str]:
        """Object ids present on the trim currently shown in the moderngl
        preview -- i.e. the config chosen in the Preview output dropdown. This
        indicates which parts the converted trim actually uses; it is NOT
        affected by the viewer Visible/Solo toggles (those only filter what is
        drawn). Ground truth is the built scene's mesh groups (keyed by object
        id, already excluding inactive/geometry-less rows for this config)."""
        scene = getattr(self.viewer, "scene", None) if self.viewer is not None else None
        groups = getattr(scene, "groups", None)
        if groups:
            # Temporarily-shown inactive parts (scene.extra) are in the scene
            # but not part of the previewed trim; they are never Active.
            return set(groups.keys()) - set(getattr(scene, "extra", ()) or ())
        # No GPU scene yet (box-viewer fallback, or the preview is still
        # building): resolve the previewed config's meshes directly. Roles are
        # cached per config on the context, so this stays cheap.
        if self.context is None:
            return set()
        config = self._mesh_scene_config()
        if config is None:
            return set()
        try:
            _flex, _props, all_meshes = core.selected_mesh_roles(self.context, [config])
        except Exception:
            return set()
        return {mesh for mesh in all_meshes if mesh in self.context.objects}

    def _selected_extra_preview_ids(self) -> list[str]:
        """Selected table parts NOT used by the previewed config. These get
        temporarily injected into the GPU scene so selecting an inactive part
        still shows it; deselecting removes it again. Active parts are never
        in this list, so their behaviour is unchanged."""
        if self.context is None or not hasattr(self, "part_tree"):
            return []
        config = self._mesh_scene_config()
        if config is None:
            return []
        try:
            _flex, _props, all_meshes = core.selected_mesh_roles(self.context, [config])
        except Exception:
            return []
        return sorted(
            object_id
            for object_id in self.part_tree.selection()
            if object_id in self.context.objects and object_id not in all_meshes
        )

    def _refresh_active_cells(self) -> None:
        """Update the parts table Active (Y/N) column for every displayed row to
        reflect the trim currently shown in the moderngl preview."""
        if not hasattr(self, "part_tree") or self.part_tree is None:
            return
        active_ids = self._preview_active_ids()
        for object_id in self.part_tree.get_children():
            self.part_tree.set(object_id, "active", yn_label(object_id in active_ids))

    def _refresh_delta_label(self) -> None:
        if self.context is None:
            self.auto_delta_var.set("")
            return
        auto = core.auto_delta_magnitude(self.context, self.conversion)
        source_refs = core.auto_delta_source_refs(self.context, self.conversion)
        if source_refs:
            names = ", ".join(self._part_display_name(object_id) for object_id in source_refs)
            source = f"found using {names}"
        else:
            # No steering ref selected (or the selected one has no usable
            # off-center X), so the auto delta is just its default.
            source = "no steering ref found"
        self.auto_delta_var.set(f"{fmt_float(auto)} ({source})")

    def _update_detail(self) -> None:
        if self.context is None:
            self.detail_var.set("")
            return
        # Every conversion mutation (mode, translate offset, structural pairing,
        # steering ref, manual delta, variant hand override) funnels through here
        # as its final UI step, so this is where we keep the GPU preview live.
        # _schedule_mesh_scene is snapshot-guarded: pure selection/visibility
        # changes leave the fingerprint unchanged and cost only a cheap compare.
        self._schedule_mesh_scene()
        selected_parts = self.part_tree.selection()
        if selected_parts:
            object_id = selected_parts[0]
            obj = self.context.objects.get(object_id)
            settings = self.conversion.get("parts", {}).get(object_id, {})
            if obj:
                display_name = self._part_display_name(object_id)
                mode = str(settings.get("mode", core.MODE_SKIP)) if isinstance(settings, dict) else core.MODE_SKIP
                part_offset = (
                    offset_display(
                        mode,
                        settings.get("translateOffset") if isinstance(settings, dict) else None,
                        manual_delta=self.manual_delta_enabled.get(),
                    )
                    if mode == core.MODE_TRANSLATE
                    else "N/A"
                )
                self.detail_var.set(
                    f"{display_name}: {mode_label(mode)}, "
                    f"full id {object_id}, x {fmt_float(obj.x)}, offset {part_offset}, dae {obj.dae_path}"
                )
                return
        active = len(core.active_part_modes(self.conversion))
        selected_variants = len(self._selected_variant_names())
        self.detail_var.set(
            f"{len(self.current_part_ids)} displayed part(s), {active} transformed part setting(s), "
            f"{selected_variants} selected variant(s)"
        )

    def _set_all_variants_selected(self, selected: bool) -> None:
        if self.context is None:
            return
        variants = self.conversion.setdefault("variants", {})
        for config_name in self.context.variants:
            settings = variants.setdefault(config_name, {})
            if isinstance(settings, dict):
                settings["selected"] = selected
        self._refresh_variants()
        self._schedule_parts_refresh(reset_view=True)
        self._refresh_delta_label()
        self._update_detail()
        self.status_var.set(
            f"{'All trims selected' if selected else 'All trims cleared'}; updating used parts..."
        )

    def _toggle_variant_selected(self, config_name: str) -> None:
        variants = self.conversion.setdefault("variants", {})
        settings = variants.setdefault(config_name, {})
        if isinstance(settings, dict):
            settings["selected"] = not bool(settings.get("selected"))
        self._refresh_variants()
        self._schedule_parts_refresh(reset_view=True)
        self._refresh_delta_label()
        self._update_detail()
        state = "selected" if self._get_variant_setting(config_name, "selected", False) else "cleared"
        self.status_var.set(f"{config_name} {state}; updating used parts...")

    def _variant_click(self, event: tk.Event) -> None:
        if not self._tree_body_click(self.variant_tree, event):
            return None
        item = self.variant_tree.identify_row(event.y)
        column = self.variant_tree.identify_column(event.x)
        if not item or self.context is None:
            return
        name = self._tree_column_name(self.variant_tree, column)
        if name == "override":
            self._edit_tree_combo(
                self.variant_tree,
                item,
                column,
                list(core.HAND_CHOICES),
                self._get_variant_setting(item, "sourceHandOverride", core.HAND_AUTO),
                lambda value: self._set_variant_setting(item, "sourceHandOverride", value),
            )
            return "break"
        if name is not None:
            # Any other data column (Use / Config / Display / Detected / Output)
            # toggles the trim's selected state.
            self._toggle_variant_selected(item)
            return "break"
        return None

    def _variant_double_click(self, event: tk.Event) -> None:
        if not self._tree_body_click(self.variant_tree, event):
            return None
        item = self.variant_tree.identify_row(event.y)
        column = self.variant_tree.identify_column(event.x)
        if not item:
            return
        name = self._tree_column_name(self.variant_tree, column)
        if name == "override":
            self._edit_tree_combo(
                self.variant_tree,
                item,
                column,
                list(core.HAND_CHOICES),
                self._get_variant_setting(item, "sourceHandOverride", core.HAND_AUTO),
                lambda value: self._set_variant_setting(item, "sourceHandOverride", value),
            )
        elif name == "output":
            return "break"

    def _part_option_label(self, object_id: str) -> str:
        display = self._part_display_name(object_id)
        if display == object_id:
            return object_id
        return f"{display} ({object_id})"

    def _name_pair_candidate(self, object_id: str, candidates: list[str]) -> str | None:
        lower_to_id = {candidate.lower(): candidate for candidate in candidates}
        pairs = (
            ("_FL", "_FR"),
            ("_FR", "_FL"),
            ("_RL", "_RR"),
            ("_RR", "_RL"),
            ("_left", "_right"),
            ("_right", "_left"),
            ("_driver", "_passenger"),
            ("_passenger", "_driver"),
            ("_L", "_R"),
            ("_R", "_L"),
            ("-L", "-R"),
            ("-R", "-L"),
            (".L", ".R"),
            (".R", ".L"),
        )
        lowered = object_id.lower()
        for old, new in pairs:
            old_lower = old.lower()
            if old_lower not in lowered:
                continue
            candidate_lower = lowered.replace(old_lower, new.lower(), 1)
            if candidate_lower in lower_to_id:
                return lower_to_id[candidate_lower]
        return None

    def _geometry_pair_candidate(self, object_id: str, candidates: list[str]) -> str | None:
        if self.context is None or object_id not in self.context.objects:
            return None
        obj = self.context.objects[object_id]
        best: tuple[float, str] | None = None
        for candidate in candidates:
            if candidate == object_id:
                continue
            other = self.context.objects.get(candidate)
            if other is None:
                continue
            if abs(obj.x) > 0.02 and abs(other.x) > 0.02 and obj.x * other.x > 0:
                continue
            score = (
                abs(obj.x + other.x) * 4.0
                + abs(obj.y - other.y)
                + abs(obj.z - other.z)
                + (0.0 if obj.dae_path == other.dae_path else 0.5)
            )
            if best is None or score < best[0]:
                best = (score, candidate)
        return best[1] if best is not None else None

    def _structural_candidate_ids(self, object_id: str) -> list[str]:
        if self.context is None:
            return []
        base_ids = self.resolved_part_ids or list(self.context.objects)
        seen: set[str] = set()
        out: list[str] = []
        for candidate in base_ids:
            if candidate == object_id or candidate not in self.context.objects or candidate in seen:
                continue
            out.append(candidate)
            seen.add(candidate)
        out.sort(key=lambda item: self._part_display_name(item).lower())
        return out

    def _suggest_structural_source(self, object_id: str, candidates: list[str]) -> str | None:
        return self._name_pair_candidate(object_id, candidates) or self._geometry_pair_candidate(
            object_id,
            candidates,
        )

    def _choose_structural_source(self, object_id: str) -> str | None:
        if self.context is None:
            return None
        candidates = self._structural_candidate_ids(object_id)
        existing = str(self._get_part_setting(object_id, "mirrorSource", "") or "")
        if existing and existing in self.context.objects and existing != object_id and existing not in candidates:
            candidates.append(existing)
        suggested = self._suggest_structural_source(object_id, candidates)
        if suggested and suggested not in candidates:
            candidates.append(suggested)
        if not candidates:
            self._show_error("Mirror Structural", "No other used mesh is available to mirror from.")
            return None

        value_by_label = {self._part_option_label(candidate): candidate for candidate in candidates}
        label_by_value = {value: label for label, value in value_by_label.items()}

        modal = tk.Toplevel(self)
        modal.title("Mirror Structural")
        modal.transient(self)
        modal.resizable(False, False)
        modal.columnconfigure(1, weight=1)

        ttk.Label(modal, text="Part").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        ttk.Label(modal, text=self._part_option_label(object_id)).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(0, 10),
            pady=(10, 4),
        )
        ttk.Label(modal, text="Swap with").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        source_var = tk.StringVar()
        initial = existing if existing in label_by_value else suggested
        if initial in label_by_value:
            source_var.set(label_by_value[initial])
        else:
            source_var.set(self._part_option_label(candidates[0]))
        combo = ttk.Combobox(
            modal,
            textvariable=source_var,
            values=list(value_by_label),
            state="readonly",
            width=72,
        )
        combo.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)

        suggestion_text = (
            f"Suggested: {self._part_option_label(suggested)}"
            if suggested
            else "Suggested: no obvious pair found"
        )
        ttk.Label(modal, text=suggestion_text).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="w",
            padx=10,
            pady=(0, 8),
        )

        result: dict[str, str | None] = {"source": None}

        def use_suggested() -> None:
            if suggested and suggested in label_by_value:
                source_var.set(label_by_value[suggested])

        def commit() -> None:
            selected = value_by_label.get(source_var.get())
            if not selected:
                self._show_error("Mirror Structural", "Select a source mesh to mirror from.", parent=modal)
                return
            if selected == object_id:
                self._show_error(
                    "Mirror Structural",
                    "A mesh cannot structurally mirror from itself.",
                    parent=modal,
                )
                return
            result["source"] = selected
            modal.destroy()

        def cancel() -> None:
            modal.destroy()

        buttons = ttk.Frame(modal)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Use Suggested", command=use_suggested).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="OK", command=commit).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="left")

        modal.bind("<Return>", lambda _event: commit())
        modal.bind("<Escape>", lambda _event: cancel())
        self._place_modal_on_app_monitor(modal)
        modal.grab_set()
        combo.focus_set()
        self.wait_window(modal)
        return result["source"]

    def _open_recommendations_modal(self) -> None:
        if self.context is None:
            self._show_error("No source", "Open a vehicle zip first.")
            return
        object_ids = list(self.resolved_part_ids or self.current_part_ids)
        if not object_ids:
            self._show_error(
                "No parts",
                "Select one or more variants and wait for the used-parts list to finish loading.",
            )
            return

        if self.recommendation_modal is not None and self.recommendation_modal.winfo_exists():
            self.recommendation_modal.lift()
            return

        self.recommendation_seq += 1
        seq = self.recommendation_seq
        self.recommendation_rows = {}

        modal = tk.Toplevel(self)
        self.recommendation_modal = modal
        modal.title("Recommended Part Modes")
        modal.transient(self)
        modal.geometry("1040x560")
        modal.minsize(820, 420)
        modal.columnconfigure(0, weight=1)
        modal.rowconfigure(2, weight=1)

        top = ttk.Frame(modal, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(3, weight=1)
        select_all_button = ttk.Button(top, text="Select All", command=lambda: self._set_all_recommendations(True))
        clear_all_button = ttk.Button(top, text="Clear All", command=lambda: self._set_all_recommendations(False))
        self.apply_recommendations_button = ttk.Button(
            top,
            text="Apply Selected",
            command=self._apply_selected_recommendations,
            state="disabled",
        )
        select_all_button.grid(row=0, column=0, sticky="w")
        clear_all_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.apply_recommendations_button.grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Button(top, text="Close", command=modal.destroy).grid(row=0, column=4, sticky="e")

        self.recommendation_status_var = tk.StringVar(value="Finding recommendations...")
        ttk.Label(modal, textvariable=self.recommendation_status_var, padding=(10, 0, 10, 4)).grid(
            row=1,
            column=0,
            sticky="ew",
        )

        frame = ttk.Frame(modal, padding=(10, 0, 10, 10))
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = ("apply", "mode", "part", "source", "current", "reason")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        self.recommendation_tree = tree
        headings = {
            "apply": "Selected",
            "mode": "Recommended",
            "part": "Part",
            "source": "Pair / Source",
            "current": "Current",
            "reason": "Reason",
        }
        widths = {
            "apply": 54,
            "mode": 132,
            "part": 290,
            "source": 250,
            "current": 190,
            "reason": 220,
        }
        for column in columns:
            tree.heading(
                column,
                text=headings[column],
                anchor="w",
            )
            tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column in {"part", "reason"},
                anchor="center" if column == "apply" else "w",
            )
        self._register_tree_headings(tree, headings)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self._configure_tree_rows(tree)
        tree.bind("<Button-1>", self._recommendation_click)

        select_all_button.configure(state="disabled")
        clear_all_button.configure(state="disabled")
        self.recommendation_select_all_button = select_all_button
        self.recommendation_clear_all_button = clear_all_button

        def closed() -> None:
            self.recommendation_seq += 1
            self.recommendation_modal = None
            self._tree_sort.pop(tree, None)
            self._tree_heading_text.pop(tree, None)
            self.recommendation_tree = None
            self.recommendation_rows = {}
            modal.destroy()

        modal.protocol("WM_DELETE_WINDOW", closed)
        modal.bind("<Escape>", lambda _event: closed())
        self._place_modal_on_app_monitor(modal)

        worker = threading.Thread(
            target=self._recommendations_worker,
            args=(seq, self.context, object_ids),
            daemon=True,
        )
        worker.start()
        modal.grab_set()
        modal.focus_set()

    def _recommendations_worker(
        self,
        seq: int,
        context: core.VehicleContext,
        object_ids: list[str],
    ) -> None:
        try:
            recommendations = build_mode_recommendations(context, object_ids)
            self.worker_queue.put(("recommendations_success", (seq, context, recommendations)))
        except Exception as exc:
            self.worker_queue.put(("recommendations_error", (seq, exc)))

    def _handle_recommendations_success(self, payload: object) -> None:
        seq, context, recommendations = payload
        if seq != self.recommendation_seq or context is not self.context:
            return
        modal = self.recommendation_modal
        tree = self.recommendation_tree
        if modal is None or tree is None or not modal.winfo_exists():
            return
        for item in tree.get_children():
            tree.delete(item)
        self.recommendation_rows = {}
        for index, recommendation in enumerate(recommendations):
            row_id = f"rec_{index}"
            self.recommendation_rows[row_id] = recommendation
            object_id = recommendation["object_id"]
            source_id = recommendation.get("source_id", "")
            current = self._recommendation_current_label(recommendation)
            tree.insert(
                "",
                "end",
                iid=row_id,
                tags=self._row_tags(index),
                values=(
                    "Y",
                    mode_label(recommendation["mode"]),
                    self._part_option_label(object_id),
                    self._part_option_label(source_id) if source_id else "",
                    current,
                    recommendation.get("reason", ""),
                ),
            )
        count = len(recommendations)
        self.recommendation_status_var.set(
            f"{count} recommendation(s) found for {len(self.resolved_part_ids or self.current_part_ids)} used part(s)."
        )
        state = "normal" if count else "disabled"
        self.recommendation_select_all_button.configure(state=state)
        self.recommendation_clear_all_button.configure(state=state)
        self.apply_recommendations_button.configure(state=state)

    def _handle_recommendations_error(self, payload: object) -> None:
        seq, exc = payload
        if seq != self.recommendation_seq:
            return
        if self.recommendation_modal is not None and self.recommendation_modal.winfo_exists():
            self.recommendation_status_var.set("Recommendation scan failed.")
        self._show_error("Recommendations failed", str(exc))

    def _recommendation_current_label(self, recommendation: dict[str, str]) -> str:
        object_id = recommendation["object_id"]
        mode = str(self._get_part_setting(object_id, "mode", core.MODE_SKIP))
        source_id = recommendation.get("source_id", "")
        if not source_id:
            return mode_label(mode)
        source_mode = str(self._get_part_setting(source_id, "mode", core.MODE_SKIP))
        return f"{mode_label(mode)} / {mode_label(source_mode)}"

    def _recommendation_click(self, event: tk.Event) -> str | None:
        tree = self.recommendation_tree
        if tree is None:
            return None
        if not self._tree_body_click(tree, event):
            return None
        item = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not item or self._tree_column_name(tree, column) != "apply":
            return None
        current = str(tree.set(item, "apply"))
        tree.set(item, "apply", "N" if current == "Y" else "Y")
        return "break"

    def _set_all_recommendations(self, selected: bool) -> None:
        tree = self.recommendation_tree
        if tree is None:
            return
        value = "Y" if selected else "N"
        for item in tree.get_children():
            tree.set(item, "apply", value)

    def _apply_selected_recommendations(self) -> None:
        if self.context is None or self.recommendation_tree is None:
            return
        selected_rows = [
            self.recommendation_rows[item]
            for item in self.recommendation_tree.get_children()
            if self.recommendation_tree.set(item, "apply") == "Y"
        ]
        if not selected_rows:
            self._show_error("No recommendations selected", "Select at least one recommendation to apply.")
            return

        applied = 0
        for recommendation in selected_rows:
            mode = recommendation["mode"]
            object_id = recommendation["object_id"]
            source_id = recommendation.get("source_id", "")
            if mode == core.MODE_MIRROR_STRUCTURAL and source_id:
                self._apply_structural_pair(object_id, source_id)
                applied += 2
            else:
                self._apply_single_part_mode(object_id, mode)
                applied += 1

        self._refresh_parts()
        self._refresh_delta_label()
        self._update_detail()
        if self.recommendation_modal is not None and self.recommendation_modal.winfo_exists():
            self.recommendation_modal.destroy()
        self.recommendation_modal = None
        if self.recommendation_tree is not None:
            self._tree_sort.pop(self.recommendation_tree, None)
            self._tree_heading_text.pop(self.recommendation_tree, None)
        self.recommendation_tree = None
        self.recommendation_rows = {}
        self.status_var.set(f"Applied {len(selected_rows)} recommendation(s) to {applied} part setting(s)")

    def _apply_single_part_mode(self, object_id: str, mode: str) -> None:
        settings = self._part_settings(object_id)
        if settings.get("mode") == core.MODE_MIRROR_STRUCTURAL:
            self._clear_structural_pair(object_id)
            settings = self._part_settings(object_id)
        settings["mode"] = mode
        settings["mirrorSource"] = None

    def _apply_structural_pair(self, object_id: str, source_id: str) -> None:
        self._clear_structural_pair(object_id)
        self._clear_structural_pair(source_id)
        settings = self._part_settings(object_id)
        source_settings = self._part_settings(source_id)
        settings["mode"] = core.MODE_MIRROR_STRUCTURAL
        settings["mirrorSource"] = source_id
        source_settings["mode"] = core.MODE_MIRROR_STRUCTURAL
        source_settings["mirrorSource"] = object_id

    def _part_click(self, event: tk.Event) -> None:
        if not self._tree_body_click(self.part_tree, event):
            return None
        item = self.part_tree.identify_row(event.y)
        column = self.part_tree.identify_column(event.x)
        if not item:
            return None
        name = self._tree_column_name(self.part_tree, column)
        if name == "visible":
            self._toggle_part_bool(item, "viewerVisible", default=True)
            return "break"
        if name == "solo":
            self._toggle_part_bool(item, "viewerSolo")
            return "break"
        if name == "mode":
            self.part_tree.focus(item)
            self.part_tree.selection_set(item)
            self._cycle_part_mode(item, 1)
            return "break"
        if name == "offset":
            if self._get_part_setting(item, "mode", core.MODE_SKIP) != core.MODE_TRANSLATE:
                self.status_var.set("Offset X only applies to Translate mode")
                return "break"
            self._edit_tree_entry(
                self.part_tree,
                item,
                column,
                offset_label(self._get_part_setting(item, "translateOffset", None)),
                lambda value: self._set_part_offset(item, value),
            )
            return "break"
        if name == "steering":
            self._set_single_steering_ref(item)
            return "break"
        # "active" is read-only, and #0/coords fall through to default row select.
        return None

    def _part_right_click(self, event: tk.Event) -> None:
        if not self._tree_body_click(self.part_tree, event):
            return None
        item = self.part_tree.identify_row(event.y)
        column = self.part_tree.identify_column(event.x)
        if not item:
            return None
        if self._tree_column_name(self.part_tree, column) == "mode":
            self.part_tree.focus(item)
            self.part_tree.selection_set(item)
            self._cycle_part_mode(item, -1)
            return "break"
        return None

    def _part_motion(self, event: tk.Event) -> None:
        if self.structural_prompt_part_id is None or self.structural_prompt_open:
            return
        item = self.part_tree.identify_row(event.y)
        column = self.part_tree.identify_column(event.x)
        if item != self.structural_prompt_part_id or self._tree_column_name(self.part_tree, column) != "mode":
            self._trigger_structural_prompt()

    def _part_leave(self, _event: tk.Event) -> None:
        if self.structural_prompt_part_id is not None and not self.structural_prompt_open:
            self._trigger_structural_prompt()

    def _part_double_click(self, event: tk.Event) -> None:
        if not self._tree_body_click(self.part_tree, event):
            return None
        item = self.part_tree.identify_row(event.y)
        column = self.part_tree.identify_column(event.x)
        if not item:
            return
        name = self._tree_column_name(self.part_tree, column)
        if name == "mode":
            return "break"
        elif name == "offset":
            if self._get_part_setting(item, "mode", core.MODE_SKIP) != core.MODE_TRANSLATE:
                self.status_var.set("Offset X only applies to Translate mode")
                return
            self._edit_tree_entry(
                self.part_tree,
                item,
                column,
                offset_label(self._get_part_setting(item, "translateOffset", None)),
                lambda value: self._set_part_offset(item, value),
            )

    def _cycle_part_mode(self, object_id: str, direction: int) -> None:
        current = str(self._get_part_setting(object_id, "mode", core.MODE_SKIP))
        try:
            index = MODE_CYCLE_VALUES.index(current)
        except ValueError:
            index = 0
        next_mode = MODE_CYCLE_VALUES[(index + direction) % len(MODE_CYCLE_VALUES)]
        self._set_part_mode(object_id, next_mode)

    def _cancel_structural_prompt(self, object_id: str | None = None) -> None:
        if object_id is not None and self.structural_prompt_part_id != object_id:
            return
        if self.structural_prompt_after_id is not None:
            try:
                self.after_cancel(self.structural_prompt_after_id)
            except Exception:
                pass
        self.structural_prompt_after_id = None
        self.structural_prompt_part_id = None
        self.structural_prompt_previous_mode = core.MODE_SKIP

    def _schedule_structural_prompt(self, object_id: str, previous_mode: str) -> None:
        self._cancel_structural_prompt()
        self.structural_prompt_part_id = object_id
        self.structural_prompt_previous_mode = (
            previous_mode if previous_mode in MODE_CYCLE_VALUES else core.MODE_SKIP
        )
        self.structural_prompt_after_id = self.after(STRUCTURAL_PROMPT_DELAY_MS, self._trigger_structural_prompt)
        self.status_var.set(
            f"Mirror Structural selected for {self._part_display_name(object_id)}; choose a source to complete it"
        )

    def _trigger_structural_prompt(self) -> None:
        object_id = self.structural_prompt_part_id
        previous_mode = self.structural_prompt_previous_mode
        if object_id is None or self.structural_prompt_open:
            return
        self._cancel_structural_prompt(object_id)
        if self.context is None:
            return
        settings = self._part_settings(object_id)
        if settings.get("mode") != core.MODE_MIRROR_STRUCTURAL or settings.get("mirrorSource"):
            return

        self.structural_prompt_open = True
        try:
            source_id = self._choose_structural_source(object_id)
        finally:
            self.structural_prompt_open = False

        settings = self._part_settings(object_id)
        if settings.get("mode") != core.MODE_MIRROR_STRUCTURAL:
            return
        if source_id:
            self._set_structural_pair(object_id, source_id)
            return

        restore_mode = previous_mode if previous_mode != core.MODE_MIRROR_STRUCTURAL else core.MODE_SKIP
        settings["mode"] = restore_mode
        settings["mirrorSource"] = None
        self._refresh_parts()
        self._update_detail()
        self.status_var.set(
            f"Mirror Structural cancelled for {self._part_display_name(object_id)}; restored {mode_label(restore_mode)}"
        )

    def _edit_tree_combo(
        self,
        tree: ttk.Treeview,
        item: str,
        column: str,
        values: list[str],
        current: str,
        on_commit,
    ) -> None:
        bbox = tree.bbox(item, column)
        if not bbox:
            return
        x, y, width, height = bbox
        combo = ttk.Combobox(tree, values=values, state="readonly")
        combo.set(current)
        combo.place(x=x, y=y, width=width, height=height)
        combo.focus_set()

        def commit(_event=None) -> None:
            value = combo.get()
            combo.destroy()
            on_commit(value)

        combo.bind("<<ComboboxSelected>>", commit)
        combo.bind("<FocusOut>", lambda _event: combo.destroy())
        combo.bind("<Escape>", lambda _event: combo.destroy())

    def _edit_tree_entry(
        self,
        tree: ttk.Treeview,
        item: str,
        column: str,
        current: str,
        on_commit,
    ) -> None:
        bbox = tree.bbox(item, column)
        if not bbox:
            return
        x, y, width, height = bbox
        entry = ttk.Entry(tree)
        entry.insert(0, current)
        entry.place(x=x, y=y, width=width, height=height)
        entry.focus_set()
        entry.selection_range(0, tk.END)

        committed = {"done": False}

        def commit(_event=None) -> None:
            if committed["done"]:
                return
            committed["done"] = True
            value = entry.get()
            entry.destroy()
            on_commit(value)

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        def cancel(_event=None) -> None:
            committed["done"] = True
            entry.destroy()

        entry.bind("<Escape>", cancel)

    def _get_variant_setting(self, config_name: str, key: str, default: object) -> object:
        variants = self.conversion.setdefault("variants", {})
        settings = variants.setdefault(config_name, {})
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)

    def _set_variant_setting(self, config_name: str, key: str, value: object) -> None:
        variants = self.conversion.setdefault("variants", {})
        settings = variants.setdefault(config_name, {})
        if isinstance(settings, dict):
            settings[key] = value
        self._refresh_variants()
        self._refresh_delta_label()
        self._update_detail()

    def _get_part_setting(self, object_id: str, key: str, default: object) -> object:
        parts = self.conversion.setdefault("parts", {})
        settings = parts.setdefault(object_id, {})
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)

    def _refresh_part_viewer_cells(self, object_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        parts = self.conversion.get("parts", {})
        if not isinstance(parts, dict):
            return
        for object_id in object_ids:
            if not self.part_tree.exists(object_id):
                continue
            settings = parts.get(object_id)
            if not isinstance(settings, dict):
                settings = {}
            self.part_tree.set(object_id, "visible", yn_label(settings.get("viewerVisible", True)))
            self.part_tree.set(object_id, "solo", yn_label(settings.get("viewerSolo")))

    def _toggle_part_bool(self, object_id: str, key: str, *, default: bool = False) -> None:
        parts = self.conversion.setdefault("parts", {})
        settings = parts.setdefault(object_id, {})
        if isinstance(settings, dict):
            settings[key] = not bool(settings.get(key, default))
        if key in {"viewerVisible", "viewerSolo"}:
            self._refresh_part_viewer_cells([object_id])
            self._refresh_viewer()
            self._update_detail()
            return
        if key == "steeringRef":
            self._refresh_variants()
        self._refresh_parts()
        self._refresh_delta_label()
        self._update_detail()

    def _set_single_steering_ref(self, object_id: str) -> None:
        if self.context is None:
            return
        parts = self.conversion.setdefault("parts", {})
        was_selected = bool(self._get_part_setting(object_id, "steeringRef", False))
        for part_id in list(parts):
            settings = parts.get(part_id)
            if isinstance(settings, dict):
                settings["steeringRef"] = False
        settings = self._part_settings(object_id)
        settings["steeringRef"] = not was_selected
        self._invalidate_variant_detection()
        self._refresh_variants()
        self._schedule_variant_detection()
        self._refresh_parts()
        self._refresh_delta_label()
        self._update_detail()
        if settings["steeringRef"]:
            self.status_var.set(f"Steering reference set: {self._part_display_name(object_id)}")
        else:
            self.status_var.set("Steering reference cleared")

    def _set_all_parts_visible(self, visible: bool) -> None:
        if self.context is None:
            return
        object_ids = [
            object_id
            for object_id in self.current_part_ids
            if object_id in self.context.objects
        ]
        if not object_ids:
            if self.resolved_part_ids:
                self.status_var.set("No displayed parts match the current filter")
            else:
                self.status_var.set("No used parts are loaded yet")
            return
        for object_id in object_ids:
            settings = self._part_settings(object_id)
            settings["viewerVisible"] = visible
            settings["viewerSolo"] = False
        self._refresh_part_viewer_cells(object_ids)
        self._refresh_viewer()
        self._update_detail()
        state = "visible" if visible else "hidden"
        scope = "displayed" if self.filter_var.get().strip() else "used"
        self.status_var.set(f"Set {len(object_ids)} {scope} part(s) {state}; cleared solo flags")

    def _toggle_selected_parts_visibility_shortcut(self, event: tk.Event) -> str | None:
        focus = self.focus_get()
        if focus is not None and focus.winfo_class() in {
            "Entry",
            "TEntry",
            "Text",
            "Combobox",
            "TCombobox",
            "Spinbox",
            "TSpinbox",
        }:
            return None
        if self.context is None:
            return None
        selected = [
            object_id
            for object_id in self.part_tree.selection()
            if self.part_tree.exists(object_id) and object_id in self.context.objects
        ]
        if not selected:
            return None
        for object_id in selected:
            settings = self._part_settings(object_id)
            settings["viewerVisible"] = not bool(settings.get("viewerVisible", True))
        self._refresh_part_viewer_cells(selected)
        self._refresh_viewer()
        self._update_detail()
        if len(selected) == 1:
            object_id = selected[0]
            visible = bool(self._get_part_setting(object_id, "viewerVisible", True))
            self.status_var.set(
                f"{self._part_display_name(object_id)} {'visible' if visible else 'hidden'}"
            )
        else:
            self.status_var.set(f"Toggled visibility for {len(selected)} selected part(s)")
        return "break"

    def _cycle_selected_part_mode_shortcut(self, _event: tk.Event, direction: int = 1) -> str | None:
        focus = self.focus_get()
        if focus is not None and focus.winfo_class() in {
            "Entry",
            "TEntry",
            "Text",
            "Combobox",
            "TCombobox",
            "Spinbox",
            "TSpinbox",
            "Button",
            "TButton",
            "Checkbutton",
            "TCheckbutton",
            "Radiobutton",
            "TRadiobutton",
        }:
            return None
        if self.context is None:
            return None
        item = self.part_tree.focus()
        if not item or not self.part_tree.exists(item) or item not in self.context.objects:
            selected = [
                object_id
                for object_id in self.part_tree.selection()
                if self.part_tree.exists(object_id) and object_id in self.context.objects
            ]
            if len(selected) != 1:
                return None
            item = selected[0]
        self._cycle_part_mode(item, direction)
        mode = str(self._get_part_setting(item, "mode", core.MODE_SKIP))
        if mode != core.MODE_MIRROR_STRUCTURAL:
            # Mirror Structural sets its own "choose a source" status message.
            self.status_var.set(f"{self._part_display_name(item)}: {mode_label(mode)}")
        return "break"

    def _part_settings(self, object_id: str) -> dict[str, object]:
        parts = self.conversion.setdefault("parts", {})
        settings = parts.setdefault(
            object_id,
            {
                "mode": core.MODE_SKIP,
                "mirrorSource": None,
                "translateOffset": None,
                "steeringRef": False,
                "viewerVisible": True,
                "viewerSolo": False,
            },
        )
        if not isinstance(settings, dict):
            settings = {}
            parts[object_id] = settings
        return settings

    def _clear_structural_pair(self, object_id: str) -> None:
        settings = self._part_settings(object_id)
        source_id = str(settings.get("mirrorSource") or "")
        settings["mirrorSource"] = None
        if not source_id:
            return
        source_settings = self._part_settings(source_id)
        if (
            source_settings.get("mode") == core.MODE_MIRROR_STRUCTURAL
            and str(source_settings.get("mirrorSource") or "") == object_id
        ):
            source_settings["mode"] = core.MODE_SKIP
            source_settings["mirrorSource"] = None

    def _set_structural_pair(self, object_id: str, source_id: str) -> None:
        self._cancel_structural_prompt(object_id)
        self._cancel_structural_prompt(source_id)
        self._clear_structural_pair(object_id)
        self._clear_structural_pair(source_id)
        settings = self._part_settings(object_id)
        source_settings = self._part_settings(source_id)
        settings["mode"] = core.MODE_MIRROR_STRUCTURAL
        settings["mirrorSource"] = source_id
        source_settings["mode"] = core.MODE_MIRROR_STRUCTURAL
        source_settings["mirrorSource"] = object_id
        self._refresh_parts()
        self._update_detail()
        self.status_var.set(
            f"Structural mirror pair set: {self._part_display_name(object_id)} <-> "
            f"{self._part_display_name(source_id)}"
        )

    def _set_part_mode(self, object_id: str, mode: str) -> None:
        current_mode = str(self._get_part_setting(object_id, "mode", core.MODE_SKIP))
        if mode == core.MODE_MIRROR_STRUCTURAL:
            settings = self._part_settings(object_id)
            if current_mode == core.MODE_MIRROR_STRUCTURAL:
                self._clear_structural_pair(object_id)
                settings = self._part_settings(object_id)
            settings["mode"] = core.MODE_MIRROR_STRUCTURAL
            settings["mirrorSource"] = None
            self._refresh_parts()
            self._update_detail()
            self._schedule_structural_prompt(object_id, current_mode)
            return
        self._cancel_structural_prompt(object_id)
        settings = self._part_settings(object_id)
        if settings.get("mode") == core.MODE_MIRROR_STRUCTURAL:
            self._clear_structural_pair(object_id)
            settings = self._part_settings(object_id)
        settings["mode"] = mode
        settings["mirrorSource"] = None
        self._refresh_parts()
        self._update_detail()

    def _set_part_offset(self, object_id: str, value: str) -> None:
        parts = self.conversion.setdefault("parts", {})
        settings = parts.setdefault(object_id, {})
        if not isinstance(settings, dict):
            return
        cleaned = value.strip()
        if not cleaned:
            settings["translateOffset"] = None
        else:
            try:
                settings["translateOffset"] = abs(float(cleaned))
            except ValueError:
                self._show_error("Invalid offset", "Part offset must be blank or a number.")
                return
        self._refresh_parts()
        self._refresh_delta_label()
        self._update_detail()

    def _part_selection_changed(self) -> None:
        self._refresh_viewer()
        self._update_detail()

    def _on_preview_pick(self, object_id: object) -> None:
        """A part was clicked in the GPU preview. object_id is the picked mesh
        name (== a part_tree iid) or None for empty space. Setting the tree
        selection fires <<TreeviewSelect>> which refreshes the highlight+detail."""
        if self.context is None:
            return
        if not object_id:
            if self.part_tree.selection():
                self.part_tree.selection_set([])  # empty click -> deselect
            return
        object_id = str(object_id)
        # The clicked part is rendered but may be filtered out of the table;
        # clear the filter so its row exists and can be selected. The filter_var
        # write-trace rebuilds the table synchronously.
        if not self.part_tree.exists(object_id) and self.filter_var.get().strip():
            self.filter_var.set("")
        if self.part_tree.exists(object_id):
            self.part_tree.selection_set([object_id])
            self.part_tree.focus(object_id)
            self.part_tree.see(object_id)

    def _manual_delta_toggled(self, *, refresh: bool = True) -> None:
        state = "normal" if self.manual_delta_enabled.get() else "disabled"
        self.manual_delta_entry.configure(state=state)
        delta = self.conversion.setdefault("delta", {})
        if isinstance(delta, dict):
            delta["manual"] = bool(self.manual_delta_enabled.get())
        if refresh:
            self._commit_delta_from_ui()

    def _commit_delta_from_ui(self) -> None:
        delta = self.conversion.setdefault("delta", {})
        if isinstance(delta, dict):
            delta["manual"] = bool(self.manual_delta_enabled.get())
            if self.manual_delta_enabled.get():
                text = self.manual_delta_var.get().strip()
                try:
                    delta["magnitude"] = abs(float(text)) if text else 0.0
                except ValueError:
                    self._show_error("Invalid delta", "Manual delta magnitude must be a number.")
                    return
        self._refresh_delta_label()
        self._refresh_parts()
        self._update_detail()

    def _browse_mods_folder(self) -> None:
        initial = existing_initial_dir(
            self.settings.get("lastModsFolder") or self.mods_folder_var.get(),
            core.WORKSPACE_DIR,
        )
        path = self._ask_directory(title="Select BeamNG mods folder", initialdir=initial)
        if path:
            self.mods_folder_var.set(path)
            self.settings["lastModsFolder"] = path
            self._save_app_settings_from_ui()

    def _browse_blender(self) -> None:
        initial = existing_initial_dir(
            self.settings.get("lastBlenderFolder") or self.blender_var.get(),
            Path(r"C:\Program Files"),
        )
        path = self._ask_open_filename(
            title="Select blender.exe",
            initialdir=initial,
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if path:
            self.blender_var.set(path)
            self.settings["lastBlenderFolder"] = str(Path(path).parent)
            self._save_app_settings_from_ui()

    def _save_app_settings_from_ui(self) -> None:
        mods_folder = self.mods_folder_var.get().strip()
        blender_exe = self.blender_var.get().strip()
        self.settings["modsFolder"] = mods_folder
        self.settings["blenderExecutable"] = blender_exe
        if mods_folder:
            self.settings["lastModsFolder"] = mods_folder
        if blender_exe:
            self.settings["lastBlenderFolder"] = str(Path(blender_exe).parent)
        core.save_app_settings(self.settings)

    def _save_config(self) -> None:
        if self.context is None:
            return
        try:
            self._commit_delta_from_ui()
            path = core.save_conversion(self.context, self.conversion)
            self._save_app_settings_from_ui()
            self.status_var.set(f"Saved config: {path}")
        except Exception as exc:
            self._show_error("Save failed", str(exc))

    def _import_config_dialog(self) -> None:
        if self.context is None:
            return
        path = self._ask_open_filename(
            title="Import conversion config",
            initialdir=str(core.PROJECTS_DIR),
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            imported = json.loads(Path(path).read_text(encoding="utf-8"))
            self.conversion, counts = core.import_matching_conversion(
                self.context,
                self.conversion,
                imported,
            )
            self._sync_delta_to_ui()
            self._invalidate_variant_detection()
            self._refresh_all(reset_view=True)
            self._schedule_variant_detection()
            self.status_var.set(
                "Imported matched settings: "
                f"{counts['variantImported']} variant(s), {counts['partImported']} part(s); "
                f"dropped {counts['variantSkipped']} variant(s), {counts['partSkipped']} part(s)"
            )
        except Exception as exc:
            self._show_error("Import failed", str(exc))

    def _set_busy(self, busy: bool) -> None:
        self.worker_running = busy
        state = "disabled" if busy else "normal"
        self.install_button.configure(state=state)
        self.blender_button.configure(state=state)
        if hasattr(self, "preview_output_combo"):
            if busy or not self.preview_output_to_config:
                self.preview_output_combo.configure(state="disabled")
            else:
                self.preview_output_combo.configure(state="readonly")

    def _start_build(self, *, install: bool) -> None:
        if self.context is None:
            self._show_error("No source", "Open a vehicle zip first.")
            return
        if install and not self.mods_folder_var.get().strip():
            self._show_error("No mods folder", "Set a BeamNG mods folder before installing.")
            return
        self._commit_delta_from_ui()
        self._save_app_settings_from_ui()
        self._set_busy(True)
        self.status_var.set("Building conversion zip...")
        worker = threading.Thread(target=self._build_worker, args=(install,), daemon=True)
        worker.start()

    def _build_worker(self, install: bool) -> None:
        assert self.context is not None
        try:
            result = core.build_batch(
                self.context,
                self.conversion,
                write_zip=True,
                install=install,
                mods_folder=Path(self.mods_folder_var.get()) if install else None,
            )
            self.worker_queue.put(("build_success", result))
        except Exception as exc:
            self.worker_queue.put(("error", exc))

    def _start_blender_preview(self) -> None:
        if self.context is None:
            self._show_error("No source", "Open a vehicle zip first.")
            return
        blender = self._resolve_blender()
        if blender is None:
            self._show_error("Blender not found", "Set the Blender executable path first.")
            return
        output_name = self.preview_output_var.get().strip()
        if output_name not in self.preview_output_to_config:
            self._show_error(
                "No output",
                "Select a buildable output config in the Preview output dropdown.",
            )
            return
        self._commit_delta_from_ui()
        self._save_app_settings_from_ui()
        self._set_busy(True)
        self.status_var.set(f"Preparing Blender preview for {output_name}...")
        worker = threading.Thread(
            target=self._blender_preview_worker,
            args=(blender, output_name),
            daemon=True,
        )
        worker.start()

    def _resolve_blender(self) -> Path | None:
        configured = self.blender_var.get().strip()
        if configured and Path(configured).exists():
            return Path(configured)
        for candidate in BLENDER_CANDIDATES:
            if candidate.exists():
                self.blender_var.set(str(candidate))
                return candidate
        return None

    def _mesh_scene_config(self) -> str | None:
        # The dropdown's highlighted-but-unconfirmed entry wins while the
        # list is open, so trims hot-load as you scroll through them.
        label = (self.preview_output_hover or self.preview_output_var.get()).strip()
        config = self.preview_output_to_config.get(label)
        if config:
            return config
        selected = self._selected_variant_names()
        if selected:
            return selected[0]
        if self.context is not None and self.context.variants:
            return next(iter(self.context.variants))
        return None

    def _mesh_scene_snapshot(self) -> str | None:
        """Fingerprint of everything the 3D scene depends on. Viewer-only
        flags (visibility/solo) are excluded - those only filter the index
        buffer and never need a rebuild."""
        config = self._mesh_scene_config()
        if config is None:
            return None
        conversion = json.loads(json.dumps(self.conversion, default=str))
        parts = conversion.get("parts")
        if isinstance(parts, dict):
            for settings in parts.values():
                if isinstance(settings, dict):
                    settings.pop("viewerVisible", None)
                    settings.pop("viewerSolo", None)
        return json.dumps(
            {
                "config": config,
                "conversion": conversion,
                # Selected-but-inactive parts are injected into the scene, so
                # the scene must rebuild when that set changes (and only then;
                # selection moves between active parts leave it empty/equal).
                "extra": self._selected_extra_preview_ids(),
            },
            sort_keys=True,
        )

    def _schedule_mesh_scene(self, *, immediate: bool = False) -> None:
        if self.context is None or not self.viewer_supports_scene:
            return
        snapshot = self._mesh_scene_snapshot()
        if snapshot is None:
            return
        if snapshot == self.mesh_scene_hash:
            return
        if self.mesh_scene_running:
            self.mesh_scene_pending = True
            return
        if self.mesh_scene_after is not None:
            return
        if immediate:
            self._start_mesh_scene()
        else:
            self.mesh_scene_after = self.after_idle(self._start_mesh_scene)

    def _start_mesh_scene(self) -> None:
        self.mesh_scene_after = None
        if self.context is None or not self.viewer_supports_scene or self.viewer is None:
            return
        snapshot = self._mesh_scene_snapshot()
        config = self._mesh_scene_config()
        if snapshot is None or config is None:
            return
        if snapshot == self.mesh_scene_hash:
            return
        self.mesh_scene_hash = snapshot
        self.mesh_scene_seq += 1
        seq = self.mesh_scene_seq
        context = self.context
        conversion_copy = json.loads(json.dumps(self.conversion, default=str))
        self.viewer.set_message(f"building preview: {config}...")
        self.mesh_scene_running = True
        extra_meshes = tuple(self._selected_extra_preview_ids())
        future = self.part_resolver.submit(
            self._mesh_scene_worker, context, conversion_copy, config, extra_meshes
        )
        future.add_done_callback(
            lambda completed, current_seq=seq, current_snapshot=snapshot: self.worker_queue.put(
                ("mesh_scene_done", (current_seq, current_snapshot, completed))
            )
        )

    @staticmethod
    def _mesh_scene_worker(
        context: core.VehicleContext,
        conversion: dict[str, object],
        config_name: str,
        extra_meshes: tuple[str, ...] = (),
    ):
        payload = core.full_vehicle_preview_payload(
            context,
            conversion,
            config_name,
            context.project_dir / "blender_preview",
            extra_meshes=extra_meshes,
        )
        cache_dir = context.project_dir / "blender_preview" / "dae_cache" / "mesh_cache"
        return mesh_preview.build_scene(payload, cache_dir)

    def _handle_mesh_scene_done(self, payload: object) -> None:
        seq, completed_snapshot, completed = payload
        self.mesh_scene_running = False
        should_apply = (
            seq == self.mesh_scene_seq
            and completed_snapshot == self._mesh_scene_snapshot()
            and self.viewer is not None
            and self.viewer_supports_scene
        )
        try:
            scene = completed.result()
        except Exception as exc:
            if should_apply and self.viewer is not None:
                self.viewer.set_message(f"preview failed: {exc}")
            self._schedule_pending_mesh_scene()
            return
        if not should_apply:
            self._schedule_pending_mesh_scene()
            return
        assert self.viewer is not None
        self.viewer.show_scene(scene, reset_view=self.mesh_scene_reset_pending)
        self.mesh_scene_reset_pending = False
        self._refresh_viewer()
        # The previewed trim (and thus its part set) may have changed; resync
        # the Active column to the scene now on screen.
        self._refresh_active_cells()
        self._schedule_pending_mesh_scene()

    def _schedule_pending_mesh_scene(self) -> None:
        if not self.mesh_scene_pending:
            return
        self.mesh_scene_pending = False
        self._schedule_mesh_scene(immediate=True)

    @staticmethod
    def _preview_needs_generated_output(
        context: core.VehicleContext,
        conversion: dict[str, object],
        config_name: str,
    ) -> bool:
        object_modes = core.active_part_modes(conversion)
        if not object_modes:
            return False
        _flex, _props, all_meshes = core.selected_mesh_roles(context, [config_name])
        return any(mesh in all_meshes for mesh in object_modes)

    def _blender_preview_worker(self, blender: Path, output_name: str) -> None:
        assert self.context is not None
        try:
            run_dir = self.context.project_dir / "blender_preview" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
            run_dir.mkdir(parents=True, exist_ok=True)
            output_sources = core.output_config_sources(self.context, self.conversion)
            config_name = output_sources.get(output_name)
            if config_name is None:
                raise RuntimeError(f"Unknown preview output {output_name!r}")
            if self._preview_needs_generated_output(self.context, self.conversion, config_name):
                result = core.build_batch(
                    self.context,
                    self.conversion,
                    write_zip=False,
                    install=False,
                    mods_folder=None,
                )
                if output_name not in result.generated_configs:
                    raise RuntimeError(f"Output {output_name!r} was not generated by the current settings")
                payload = core.output_vehicle_preview_payload(
                    self.context,
                    self.conversion,
                    output_name,
                    result.unpacked_dir,
                    result.generated_daes,
                    run_dir,
                )
            else:
                payload = core.full_vehicle_preview_payload(
                    self.context,
                    self.conversion,
                    config_name,
                    run_dir,
                )
                payload["output_name"] = output_name
                payload["show_unchanged"] = True
            payload_path = run_dir / "blender_preview_payload.json"
            payload_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            subprocess.Popen(
                [
                    str(blender),
                    "--python",
                    str(BLENDER_PREVIEW_SCRIPT),
                    "--",
                    str(payload_path),
                ],
                cwd=str(THIS_DIR),
            )
            self.worker_queue.put(("preview_success", payload_path))
        except Exception as exc:
            self.worker_queue.put(("error", exc))

    def _poll_worker_queue(self) -> None:
        handled = False
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break
            handled = True
            self._handle_worker_message(kind, payload)
        self.after(40 if handled else 80, self._poll_worker_queue)

    def _handle_worker_message(self, kind: str, payload: object) -> None:
        if kind == "parts_success":
            self._handle_parts_success(payload)
            return
        if kind == "vehicle_load_success":
            self._handle_vehicle_load_success(payload)
            return
        if kind == "vehicle_load_error":
            self._handle_vehicle_load_error(payload)
            return
        if kind == "recommendations_success":
            self._handle_recommendations_success(payload)
            return
        if kind == "recommendations_error":
            self._handle_recommendations_error(payload)
            return
        if kind == "mesh_scene_done":
            self._handle_mesh_scene_done(payload)
            return
        if kind == "variant_hands_done":
            self._handle_variant_hands_done(payload)
            return

        self._set_busy(False)
        if kind == "build_success":
            result: core.BuildResult = payload
            if result.installed_zip:
                self.status_var.set(
                    f"Built {result.package_zip} and installed {result.installed_zip}; "
                    f"{len(result.generated_configs)} config(s)"
                )
            else:
                self.status_var.set(
                    f"Built {result.package_zip}; {len(result.generated_configs)} config(s)"
                )
        elif kind == "preview_success":
            self.status_var.set(f"Blender preview launched: {payload}")
        else:
            self._show_error("Operation failed", str(payload))
            self.status_var.set("Operation failed")
        self._refresh_all()

    def _handle_parts_success(self, payload: object) -> None:
        seq, context, reset_view, selected, future = payload
        self.part_refresh_running = False
        should_apply = seq == self.part_refresh_seq and context is self.context
        try:
            result = future.result()
        except Exception as exc:
            if should_apply:
                self.resolved_part_ids = []
                self._refresh_parts(reset_view=reset_view)
                self.status_var.set(f"Part resolver failed: {exc}")
            self._schedule_pending_parts_refresh()
            return
        if not should_apply:
            self._schedule_pending_parts_refresh()
            return
        self.resolved_part_ids = result
        core.save_cached_part_ids(context, selected, self.resolved_part_ids)
        self._refresh_parts(reset_view=reset_view)
        self._update_detail()
        self.status_var.set(f"{len(self.current_part_ids)} used part(s) displayed")
        self._schedule_pending_parts_refresh()

    def _schedule_pending_parts_refresh(self) -> None:
        if not self.part_refresh_pending:
            return
        reset_view = self.part_refresh_pending_reset
        self.part_refresh_pending = False
        self.part_refresh_pending_reset = False
        self._schedule_parts_refresh(reset_view=reset_view)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic BeamNG hand-drive visual conversion tool")
    parser.add_argument("--source", help="Vehicle source zip to open")
    parser.add_argument("--vehicle", help="Vehicle model folder under vehicles/")
    parser.add_argument("--validate", action="store_true", help="Print detected inventory and exit")
    return parser.parse_args()


def validate_source(source: Path, vehicle: str | None) -> None:
    context = core.load_vehicle_context(source, vehicle)
    conversion, loaded = core.load_or_create_conversion(context)
    print(f"Source: {context.source_zip}")
    print(f"Vehicle: {context.vehicle_id}")
    print(f"Project: {context.project_dir}")
    print(f"Project config loaded: {loaded}")
    print(f"DAE files: {len(context.dae_paths)}")
    print(f"Variants: {len(context.variants)}")
    print(f"DAE objects: {len(context.objects)}")
    print(f"Auto delta magnitude: {fmt_float(core.auto_delta_magnitude(context, conversion))}")


def main() -> None:
    args = parse_args()
    if args.validate:
        if not args.source:
            raise SystemExit("--validate requires --source")
        validate_source(Path(args.source), args.vehicle)
        return

    app = HandDriveToolApp()
    if args.source:
        app.after(50, lambda: app._load_source_zip(Path(args.source), args.vehicle))
    else:
        last_source = str(app.settings.get("lastVehicleZipPath") or "")
        last_vehicle = str(app.settings.get("lastVehicleId") or "")
        if last_source and Path(last_source).exists():
            app.after(
                50,
                lambda: app._load_source_zip(
                    Path(last_source),
                    last_vehicle or None,
                ),
            )
    app.mainloop()


if __name__ == "__main__":
    main()
