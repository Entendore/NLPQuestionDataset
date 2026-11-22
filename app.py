"""
qgen_gui.py — PySide6 GUI for the Yes/No Question Generator.

Tabs: generate from text · generate from dictionary word · manage the
downloadable dictionary · browse the question database (virtual
infinite-scroll table with search/filters, delete, export).

Extras: auto-save to database, CSV/JSON export, copy rows, cancellable
downloads/index builds with live progress, dark mode, persisted settings.

Run:  python qgen_gui.py
"""

import csv, json, os, sys, threading, urllib.request

from PySide6.QtCore import QModelIndex, Qt, QSettings, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QAction, QUrl
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QStatusBar, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

APP_NAME = "Yes/No Question Generator"
SOURCES = (
    ("wordset",    "WordSet — 34k words, ~25 MB (quick start)"),
    ("wiktionary", "Wiktionary — 700k+ words, ~1 GB (comprehensive)"),
    ("words",      "Word list only — 4 MB (spell-check)"),
)
DB_KINDS   = ("text", "definition", "pos", "hypernym", "antonym")
ANS_COLORS = {"Yes": "#2ea043", "No": "#e5534b"}

# ================================================================= workers ==
class JobCancelled(Exception):
    pass

class GenericWorker(QThread):
    """Runs job(worker) off the UI thread; reports via signals.
    Cancellation is cooperative: long-running jobs check worker.cancel_event."""
    progress  = Signal(str, int, int)      # stage, done, total (-1 = pulse)
    done      = Signal(object)
    failed    = Signal(str)
    cancelled = Signal()

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            self.done.emit(self.job(self))
        except JobCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _fetch_with_progress(url, dest, cb, cancel=None):
    """Download with progress callback + cooperative cancellation."""
    req = urllib.request.Request(url, headers={"User-Agent": "yesno-qgen-gui/1.0"})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel and cancel.is_set():
                    raise JobCancelled()
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                cb(done, total)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _gui_build(self, source, conn):
    """Replacement DictionaryStore._build: signal progress + cancellable."""
    emit = getattr(self, "_emit", lambda *a: None)
    cancel = getattr(self, "_cancel", None)
    emit("build", 0, -1)
    conn.executescript("PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF;")
    conn.executescript("DROP TABLE IF EXISTS entries;"
                       "CREATE TABLE entries (word TEXT, pos TEXT, def TEXT);")
    batch, total = [], 0

    def flush():
        nonlocal batch, total
        if batch:
            if cancel and cancel.is_set():
                raise JobCancelled()
            conn.executemany("INSERT INTO entries VALUES (?,?,?)", batch)
            total += len(batch)
            batch = []
            emit("build", total, -1)

    iters = {"wordset":    lambda: self._iter_wordset(),
             "wiktionary": lambda: self._iter_wiktionary(self._limits.get(source)),
             "words":      lambda: self._iter_words()}[source]()
    for row in iters:
        if cancel and cancel.is_set():
            raise JobCancelled()
        batch.append(row)
        if len(batch) >= 50_000:
            flush()
    flush()
    conn.execute("CREATE INDEX IF NOT EXISTS ix_word ON entries(word)")
    conn.executescript("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
                       "INSERT INTO meta VALUES ('built','1');")
    conn.commit()
    conn.executescript("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")

# ============================================================ table model ==
class QuestionTableModel(QAbstractTableModel):
    """Virtual-table model: holds only the rows fetched so far; more rows are
    appended on demand (infinite scroll). SQL row tuple order:
    (id, question, answer, kind, origin, created_at)."""
    HEADERS = ("ID", "Question", "Answer", "Kind", "Origin", "Saved")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._total = 0

    # ---- Qt model API --------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row, col = self._rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            val = row[col]
            return "" if val is None else str(val)
        if role == Qt.ForegroundRole and self.HEADERS[col] == "Answer":
            color = ANS_COLORS.get(row[2])
            return QColor(color) if color else None
        if role == Qt.TextAlignmentRole and self.HEADERS[col] == "Answer":
            return int(Qt.AlignCenter)
        return None

    # ---- data management -------------------------------------------------------
    def set_total(self, total):
        self._total = total

    def reset_rows(self):
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def append_rows(self, rows):
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    def row_id(self, row):
        return self._rows[row][0]

# ================================================================== window ==
class MainWindow(QMainWindow):
    RESULT_COLS = ("Question", "Answer", "Kind", "Origin")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1080, 740)
        self.settings = QSettings("yesno-qgen", "qgen")
        self.core = self.store = self.dictionary = self.qdb = None
        self.results = []
        self._seen = set()
        self._pending = 0
        self.workers = []

        # infinite-scroll state
        self.db_chunk = 200                    # rows per fetch
        self.db_loaded = 0                     # rows currently in the model
        self.db_total = 0                      # rows matching the active filters
        self._db_active = ("", None, None)     # filter snapshot for scroll fetches
        self._db_loading = False               # re-entrancy guard
        self._db_fills = 0                     # safety cap for viewport filling

        self._build_ui()
        self._build_menu()
        self._load_settings()
        self._load_core()

    # ------------------------------------------------------------- UI build --
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_text(), "1 · From text")
        self.tabs.addTab(self._tab_word(), "2 · From word")
        self.tabs.addTab(self._tab_dict(), "Dictionary")
        self.tabs.addTab(self._tab_db(),   "Database")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        results = QGroupBox("Results")
        rv = QVBoxLayout(results)
        head = QHBoxLayout()
        self.count_label = QLabel("0 questions")
        self.chk_autosave = QCheckBox("Auto-save")
        b_save = QPushButton("Save to database")
        b_save.setShortcut("Ctrl+S")
        b_save.clicked.connect(self.save_to_db)
        b_copy = QPushButton("Copy");         b_copy.clicked.connect(self.copy_results)
        b_csv  = QPushButton("Export CSV");   b_csv.clicked.connect(self.export_csv)
        b_json = QPushButton("Export JSON");  b_json.clicked.connect(self.export_json)
        b_clr  = QPushButton("Clear list");   b_clr.clicked.connect(self.clear_results)
        head.addWidget(self.count_label)
        head.addStretch(1)
        head.addWidget(self.chk_autosave)
        head.addWidget(b_save)
        head.addWidget(b_copy)
        head.addWidget(b_csv)
        head.addWidget(b_json)
        head.addWidget(b_clr)
        rv.addLayout(head)

        self.table = QTableWidget(0, len(self.RESULT_COLS))
        self.table.setHorizontalHeaderLabels(self.RESULT_COLS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        rv.addWidget(self.table)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(results)
        split.setSizes([400, 280])
        root.addWidget(split)

        self.status = QLabel("Starting…")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(300)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("btnCancel")
        self.btn_cancel.clicked.connect(self._cancel_all)
        self.btn_cancel.hide()
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addWidget(self.status, 1)
        sb.addPermanentWidget(self.btn_cancel)
        sb.addPermanentWidget(self.progress)

        self.busy_buttons = [getattr(self, n) for n in
                             ("btn_text", "btn_word", "btn_dl", "btn_inspect")]
        for b in self.busy_buttons:
            b.setEnabled(False)                # enabled once the core loads

    def _tab_text(self):
        page = QWidget(); v = QVBoxLayout(page)
        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText(
            "Type or paste English sentences…\n"
            "e.g.  Alice works at Google.  The cat doesn't like water.")
        v.addWidget(self.text_input)

        opts = QHBoxLayout()
        self.neg_combo = QComboBox()
        self.neg_combo.addItems(["Keep polarity", "Force negative question",
                                 "Force positive question"])
        self.chk_fallback = QCheckBox("Template fallback")
        self.chk_fallback.setChecked(True)
        self.chk_oov = QCheckBox("Skip sentences with unknown words")
        for w_ in (QLabel("Polarity:"), self.neg_combo,
                   self.chk_fallback, self.chk_oov):
            opts.addWidget(w_)
        opts.addStretch(1)
        v.addLayout(opts)

        self.btn_text = QPushButton("Generate yes/no questions  (Ctrl+Return)")
        self.btn_text.setShortcut("Ctrl+Return")
        self.btn_text.clicked.connect(self.generate_from_text)
        v.addWidget(self.btn_text)
        return page

    def _tab_word(self):
        page = QWidget(); v = QVBoxLayout(page)
        row = QHBoxLayout()
        self.word_input = QLineEdit()
        self.word_input.setPlaceholderText("dictionary word, e.g. dog")
        self.word_input.returnPressed.connect(self.generate_from_word)
        self.btn_word = QPushButton("Generate")
        self.btn_word.clicked.connect(self.generate_from_word)
        row.addWidget(QLabel("Word:"))
        row.addWidget(self.word_input, 1)
        row.addWidget(self.btn_word)
        v.addLayout(row)

        kinds = QHBoxLayout()
        self.kind_boxes = {}
        for kind, label in (("definition", "Definitions"),
                            ("pos", "Grammar (noun, conjunction…)"),
                            ("hypernym", "Categories"), ("antonym", "Antonyms")):
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.kind_boxes[kind] = cb
            kinds.addWidget(cb)
        kinds.addWidget(QLabel("Max/kind:"))
        self.max_kind = QSpinBox()
        self.max_kind.setRange(1, 6)
        self.max_kind.setValue(2)
        kinds.addWidget(self.max_kind)
        kinds.addStretch(1)
        v.addLayout(kinds)

        self.word_info = QPlainTextEdit()
        self.word_info.setReadOnly(True)
        self.word_info.setMaximumHeight(170)
        self.word_info.setPlaceholderText(
            "Word info (grammar + definitions) appears here…")
        v.addWidget(self.word_info)
        v.addStretch(1)
        return page

    def _tab_dict(self):
        page = QWidget(); v = QVBoxLayout(page)
        row = QHBoxLayout()
        self.src_combo = QComboBox()
        for key, label in SOURCES:
            self.src_combo.addItem(label, key)
        self.btn_dl = QPushButton("Download & index")
        self.btn_dl.clicked.connect(self.download_dictionary)
        row.addWidget(QLabel("Source:"))
        row.addWidget(self.src_combo, 1)
        row.addWidget(QLabel("Limit:"))
        self.dl_limit = QSpinBox()
        self.dl_limit.setRange(0, 2_000_000)
        self.dl_limit.setSingleStep(50_000)
        self.dl_limit.setValue(0)
        self.dl_limit.setToolTip("0 = full dictionary (use a small limit to test)")
        row.addWidget(self.dl_limit)
        row.addWidget(self.btn_dl)
        v.addLayout(row)
        self.dl_status = QLabel("")
        v.addWidget(self.dl_status)

        h = QHBoxLayout()
        self.inspect_input = QLineEdit()
        self.inspect_input.setPlaceholderText("look up any word…")
        self.inspect_input.returnPressed.connect(self.inspect_word)
        self.btn_inspect = QPushButton("Inspect")
        self.btn_inspect.clicked.connect(self.inspect_word)
        h.addWidget(self.inspect_input, 1)
        h.addWidget(self.btn_inspect)
        v.addLayout(h)
        self.inspect_out = QPlainTextEdit()
        self.inspect_out.setReadOnly(True)
        v.addWidget(self.inspect_out, 1)
        return page

    def _tab_db(self):
        page = QWidget(); v = QVBoxLayout(page)

        # -- filters -----------------------------------------------------------
        f1 = QHBoxLayout()
        self.db_search = QLineEdit()
        self.db_search.setPlaceholderText("search question or origin…  (Enter)")
        self.db_search.returnPressed.connect(self.refresh_db)
        self.db_kind = QComboBox()
        self.db_kind.addItem("All kinds")
        self.db_kind.addItems(DB_KINDS)
        self.db_answer = QComboBox()
        self.db_answer.addItem("All answers")
        self.db_answer.addItems(("Yes", "No"))
        for combo in (self.db_kind, self.db_answer):
            combo.currentIndexChanged.connect(self.refresh_db)
        b_refresh = QPushButton("Refresh")
        b_refresh.clicked.connect(self.refresh_db)
        f1.addWidget(QLabel("Search:"))
        f1.addWidget(self.db_search, 1)
        f1.addWidget(QLabel("Kind:"))
        f1.addWidget(self.db_kind)
        f1.addWidget(QLabel("Answer:"))
        f1.addWidget(self.db_answer)
        f1.addWidget(b_refresh)
        v.addLayout(f1)

        # -- loaded counter + fetch size ----------------------------------------
        f1b = QHBoxLayout()
        self.db_loaded_label = QLabel("Loaded 0 of 0")
        self.btn_db_top = QPushButton("⤒ Top")
        f1b.addWidget(self.db_loaded_label)
        f1b.addStretch(1)
        f1b.addWidget(self.btn_db_top)
        f1b.addWidget(QLabel("Rows/fetch:"))
        self.db_pagesize = QComboBox()
        self.db_pagesize.addItems(("100", "200", "500", "1000", "2000"))
        self.db_pagesize.setCurrentText("200")
        self.db_pagesize.currentTextChanged.connect(self._on_chunk_size)
        f1b.addWidget(self.db_pagesize)
        v.addLayout(f1b)

        # -- info + actions ------------------------------------------------------
        f2 = QHBoxLayout()
        self.db_info = QLabel("Database not opened yet.")
        b_export = QPushButton("Export all…")
        b_export.clicked.connect(self.export_db)
        b_load = QPushButton("Load into results")
        b_load.clicked.connect(self.load_from_db)
        b_copy = QPushButton("Copy")
        b_copy.clicked.connect(self.copy_db_rows)
        b_del = QPushButton("Delete selected")
        b_del.clicked.connect(self.delete_selected)
        b_clear = QPushButton("Clear all…")
        b_clear.clicked.connect(self.clear_db)
        f2.addWidget(self.db_info, 1)
        for b in (b_export, b_load, b_copy, b_del, b_clear):
            f2.addWidget(b)
        v.addLayout(f2)

        # -- virtual table ---------------------------------------------------------
        self.db_model = QuestionTableModel()
        self.db_view = QTableView()
        self.db_view.setModel(self.db_model)
        self.db_view.verticalHeader().setVisible(False)
        self.db_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.db_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.db_view.setAlternatingRowColors(True)
        self.db_view.setSortingEnabled(False)     # SQL order (newest first)
        hh = self.db_view.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionsClickable(False)
        sb = self.db_view.verticalScrollBar()
        sb.valueChanged.connect(self._db_on_scroll)   # near bottom -> fetch
        sb.rangeChanged.connect(self._db_on_range)    # can't scroll -> fill
        self.btn_db_top.clicked.connect(self.db_view.scrollToTop)
        v.addWidget(self.db_view, 1)
        return page

    def _build_menu(self):
        mb = self.menuBar()
        filem = mb.addMenu("&File")
        a = filem.addAction("Export results &CSV…")
        a.triggered.connect(self.export_csv)
        a = filem.addAction("Export results &JSON…")
        a.triggered.connect(self.export_json)
        filem.addSeparator()
        a = filem.addAction("Export &all from database…")
        a.triggered.connect(self.export_db)
        filem.addSeparator()
        a = filem.addAction("Open database &folder")
        a.triggered.connect(self.open_db_folder)
        filem.addSeparator()
        a = filem.addAction("&Quit")
        a.setShortcut("Ctrl+Q")
        a.triggered.connect(self.close)

        viewm = mb.addMenu("&View")
        self.act_dark = viewm.addAction("&Dark mode")
        self.act_dark.setCheckable(True)
        self.act_dark.toggled.connect(self.set_dark)

        helpm = mb.addMenu("&Help")
        a = helpm.addAction("&About")
        a.triggered.connect(self.show_about)

    # -------------------------------------------------------- worker plumbing --
    def _load_core(self):
        def job(worker):
            import qgen_core
            def gui_fetch(url, dest, progress=True):
                _fetch_with_progress(
                    url, dest,
                    lambda d, t: worker.progress.emit("download", d, t),
                    cancel=worker.cancel_event)
            qgen_core._fetch = gui_fetch
            qgen_core.DictionaryStore._build = _gui_build
            return qgen_core
        self._run_worker(job, self._on_core_loaded, "Loading NLP model (spaCy)…")

    def _run_worker(self, job, on_done, msg):
        w = GenericWorker(job)
        w.progress.connect(self._on_progress)
        w.done.connect(on_done)
        w.failed.connect(self._on_failed)
        w.cancelled.connect(self._on_cancelled)
        w.finished.connect(lambda: self._worker_finished(w))
        self.workers.append(w)
        self._busy(True, msg)
        w.start()

    def _worker_finished(self, w):
        w.cancel_event.clear()
        if w in self.workers:
            self.workers.remove(w)
        w.deleteLater()
        self._busy(False)

    def _cancel_all(self):
        for w in self.workers:
            w.cancel()
        self.status.setText("Cancelling…")

    def _busy(self, on, msg="Working…"):
        self._pending += 1 if on else -1
        busy = self._pending > 0
        for b in self.busy_buttons:
            b.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        self.status.setText(msg if busy else "Ready")

    def _on_progress(self, stage, done, total):
        self.progress.setVisible(True)
        if stage == "download":
            if total > 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(done * 100 / total))
                self.status.setText(
                    f"Downloading… {done/1e6:,.1f} / {total/1e6:,.1f} MB")
            else:
                self.progress.setRange(0, 0)
                self.status.setText(f"Downloading… {done/1e6:,.1f} MB")
        elif stage == "build":
            self.progress.setRange(0, 0)
            self.status.setText(
                f"Building search index… {done:,} entries (one-time)")

    def _on_failed(self, msg):
        self.status.setText("Error")
        QMessageBox.critical(self, APP_NAME, msg)

    def _on_cancelled(self):
        self.status.setText("Cancelled")

    # ------------------------------------------------------------- core ready --
    def _on_core_loaded(self, core):
        self.core = core
        self.qdb = core.QuestionDatabase()
        self.store = core.DictionaryStore()
        attached = next((k for k, _ in SOURCES if self.store.is_downloaded(k)), None)
        if attached:
            def attach(worker, src=attached):
                self.store._emit = lambda s, d, t: worker.progress.emit(s, d, t)
                self.store._cancel = worker.cancel_event
                self.store.use(src)
                return src
            self._run_worker(attach, self._on_attached, f"Opening '{attached}'…")
        else:
            self._update_dict_status(
                "No dictionary downloaded yet — WordNet fallback active.")
            self.refresh_db()
            self.status.setText("Ready — pick a dictionary on the Dictionary tab")

    def _on_attached(self, source):
        self.dictionary = self.core.EnglishDictionary(store=self.store)
        idx = next(i for i, (k, _) in enumerate(SOURCES) if k == source)
        self.src_combo.setCurrentIndex(idx)
        self._update_dict_status(f"Active dictionary: {source}")
        self.refresh_db()

    def _update_dict_status(self, note=None):
        info = ", ".join(self.store.info()) if self.store else ""
        text = f"Installed: {info}" if info else "Installed: nothing yet"
        self.dl_status.setText((note + "   |   " if note else "") + text)

    # ------------------------------------------------------------ generate UI --
    def generate_from_text(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_NAME, "Enter some sentences first.")
            return
        neg = (None, True, False)[self.neg_combo.currentIndex()]
        skip_oov = self.chk_oov.isChecked() and self.dictionary is not None

        def job(worker):
            return self.core.generate_yes_no_questions(
                text, negate=neg, use_fallback=self.chk_fallback.isChecked(),
                dictionary=self.dictionary, skip_oov=skip_oov)
        self._run_worker(job, self._add_rows, "Generating questions…")

    def generate_from_word(self):
        word = self.word_input.text().strip()
        if not word:
            return
        dictionary = self.dictionary or self.core.WORDNET
        kinds = tuple(k for k, cb in self.kind_boxes.items() if cb.isChecked())

        def job(worker):
            qs = self.core.dictionary_yes_no_questions(
                word, max_per_kind=self.max_kind.value(),
                kinds=kinds, dictionary=dictionary)
            for item in qs:
                item["word"] = word
            return {"info": self._word_info_text(dictionary, word),
                    "questions": qs}

        def ok(res):
            self.word_info.setPlainText(res["info"])
            self._add_rows(res["questions"])
        self._run_worker(job, ok, f"Looking up '{word}'…")

    @staticmethod
    def _word_info_text(dictionary, word):
        poss = sorted(dictionary.pos_names(word))
        defs = dictionary.definitions(word)
        lines = [f"“{word}”",
                 "Grammar: " + (", ".join(poss) if poss else "(not found)"), ""]
        if defs:
            lines.append("Definitions:")
            lines += [f"  • ({pos}) {gloss}" for pos, gloss in defs[:8]]
        else:
            lines.append("Definitions: none found")
        return "\n".join(lines)

    def inspect_word(self):
        word = self.inspect_input.text().strip()
        if not word:
            return
        dictionary = self.dictionary or self.core.WORDNET

        def job(worker):
            return dictionary.is_word(word), self._word_info_text(dictionary, word)

        def ok(res):
            known, text = res
            self.inspect_out.setPlainText(
                text if known else f"“{word}” is not in the dictionary.")
        self._run_worker(job, ok, "Looking up…")

    # ------------------------------------------------------------ results UI --
    def _add_rows(self, rows):
        rows = rows or []
        if not rows:
            QMessageBox.information(
                self, APP_NAME,
                "No yes/no questions could be generated from that input.")
            return
        added = 0
        self.table.setSortingEnabled(False)
        for r in rows:
            key = (r.get("question"), r.get("answer"),
                   r.get("source") or r.get("word") or "")
            if key in self._seen:
                continue
            self._seen.add(key)
            self.results.append(r)
            self._insert_row(self.table, self.RESULT_COLS, r)
            added += 1
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"{len(self.results)} questions")
        if added == 0:
            QMessageBox.information(
                self, APP_NAME,
                f"All {len(rows)} generated question(s) were duplicates.")
            return
        self.status.setText(f"Generated {added} new question(s).")
        if self.chk_autosave.isChecked() and self.qdb is not None:
            stored = self.qdb.add(rows)
            self.refresh_db()
            self.status.setText(
                f"Auto-saved {stored} new question(s) to the database.")

    @staticmethod
    def _insert_row(table, cols, r):
        row = table.rowCount()
        table.insertRow(row)
        q = r.get("question") or "(no question)"
        a = r.get("answer") or "—"
        kind = r.get("kind") or ("text" if r.get("source") else "word")
        origin = r.get("source") or r.get("word") or ""
        if r.get("oov"):
            origin = f"{origin}   [unknown: {', '.join(r['oov'])}]"
        values = (q, a, kind, origin) if len(cols) == 4 else \
                 (r.get("id", ""), q, a, kind, origin, r.get("created_at", ""))
        for col, val in enumerate(values):
            item = QTableWidgetItem(str(val))
            if cols[col] == "Answer":
                color = ANS_COLORS.get(str(val))
                if color:
                    item.setForeground(QColor(color))
                item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, col, item)

    def clear_results(self):
        self.results.clear()
        self._seen.clear()
        self.table.setRowCount(0)
        self.count_label.setText("0 questions")

    def save_to_db(self):
        if not self.results:
            QMessageBox.information(self, APP_NAME,
                                    "Generate some questions first.")
            return
        try:
            stored = self.qdb.add(self.results)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Database error: {exc}")
            return
        skipped = len(self.results) - stored
        msg = f"Saved {stored} new question(s) → {self.qdb.path}"
        if skipped:
            msg += f"  ({skipped} duplicate(s) skipped)"
        self.status.setText(msg)
        self.refresh_db()

    def copy_results(self):
        self._copy_view(self.table, self.RESULT_COLS)

    def export_csv(self):
        if not self.results:
            QMessageBox.information(self, APP_NAME, "Nothing to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV",
                                              "yesno_questions.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["question", "answer", "kind", "origin"])
            for r in self.results:
                w.writerow([r.get("question", ""), r.get("answer") or "",
                            r.get("kind", ""),
                            r.get("source") or r.get("word") or ""])
        self.status.setText(f"Exported {len(self.results)} rows → {path}")

    def export_json(self):
        if not self.results:
            QMessageBox.information(self, APP_NAME, "Nothing to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export JSON",
                                              "yesno_questions.json",
                                              "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.results, fh, ensure_ascii=False, indent=2)
        self.status.setText(f"Exported {len(self.results)} rows → {path}")

    # ---------------------------------------------------------- database tab --
    def _db_filters(self):
        kind = None if self.db_kind.currentIndex() == 0 else self.db_kind.currentText()
        ans = None if self.db_answer.currentIndex() == 0 else self.db_answer.currentText()
        return self.db_search.text().strip(), kind, ans

    def _on_chunk_size(self, text):
        self.db_chunk = int(text)          # affects future fetches only

    def _on_tab_changed(self, index):
        if index == 3:                     # Database tab opened
            QTimer.singleShot(0, self._db_fill_viewport)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._db_fill_viewport)

    def refresh_db(self):
        """Reset the virtual table: drop loaded rows, fetch the first chunk."""
        if self.qdb is None:
            return
        self._db_active = self._db_filters()      # snapshot for scroll fetches
        self.db_total = self.qdb.search_count(*self._db_active)
        self.db_loaded = 0
        self._db_fills = 0
        self.db_model.set_total(self.db_total)
        self.db_model.reset_rows()
        self._db_fetch_chunk()
        self._db_update_stats()
        self.db_view.scrollToTop()
        QTimer.singleShot(0, self._db_fill_viewport)

    def _db_fetch_chunk(self):
        """Fetch the next chunk using the filters captured at reset time."""
        if self._db_loading or self.db_loaded >= self.db_total:
            return 0
        self._db_loading = True
        try:
            rows = self.qdb.search(*self._db_active,
                                   limit=self.db_chunk, offset=self.db_loaded)
            self.db_loaded += len(rows)
            self.db_model.append_rows(rows)
            done = " (all loaded)" if self.db_loaded >= self.db_total else ""
            self.db_loaded_label.setText(
                f"Loaded {self.db_loaded:,} of {self.db_total:,} matching{done}")
            return len(rows)
        finally:
            self._db_loading = False

    def _db_on_scroll(self, value):
        sb = self.db_view.verticalScrollBar()
        if sb.maximum() > 0 and value >= sb.maximum() - 8:   # near bottom
            self._db_fetch_chunk()

    def _db_on_range(self, _min, maximum):
        if maximum == 0:            # viewport can't scroll yet -> keep filling
            QTimer.singleShot(0, self._db_fill_viewport)

    def _db_fill_viewport(self):
        """Keep fetching until the table fills its viewport and a scrollbar
        appears (or everything is loaded)."""
        if not self.db_view.isVisible() or self._db_fills > 200:
            return
        if self.db_view.verticalScrollBar().maximum() > 0:
            return
        if self._db_fetch_chunk():
            self._db_fills += 1
            QTimer.singleShot(0, self._db_fill_viewport)

    def _db_update_stats(self):
        st = self.qdb.stats()
        parts = [f"Total: {st['total']:,}"]
        parts += [f"{k}: {v:,}" for k, v in sorted(st["answers"].items()) if k]
        top = ", ".join(f"{k}: {v:,}"
                        for k, v in list(st["kinds"].items())[:3] if k)
        if top:
            parts.append(top)
        self.db_info.setText(" · ".join(parts) + f"   |   {self.qdb.path}")

    def load_from_db(self):
        """Loads ALL rows matching the current filters (ignores scrolling)."""
        if self.qdb is None:
            return
        query, kind, ans = self._db_filters()
        total = self.qdb.search_count(query, kind, ans)
        if not total:
            QMessageBox.information(self, APP_NAME, "No matching rows to load.")
            return
        if total > 50_000:
            answer = QMessageBox.question(
                self, APP_NAME,
                f"Load ALL {total:,} matching rows into the results table?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        rows = self.qdb.search(query, kind, ans, limit=total)
        self.clear_results()
        cols = ("id", "question", "answer", "kind", "origin", "created_at")
        self.table.setSortingEnabled(False)
        for r in rows:
            rec = dict(zip(cols, r))
            rec = {"question": rec["question"], "answer": rec["answer"],
                   "kind": rec["kind"], "origin": rec["origin"]}
            key = (rec["question"], rec["answer"], rec["origin"])
            if key in self._seen:
                continue
            self._seen.add(key)
            self.results.append(rec)
            self._insert_row(self.table, self.RESULT_COLS, rec)
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"{len(self.results)} questions")
        self.status.setText(f"Loaded {len(rows):,} questions from the database.")

    def copy_db_rows(self):
        self._copy_view(self.db_view, QuestionTableModel.HEADERS)

    def _copy_view(self, view, headers):
        """Copy selected (or all loaded) rows to the clipboard, TSV + header."""
        sel = view.selectionModel()
        if sel and sel.hasSelection():
            rows = sorted({ix.row() for ix in sel.selectedIndexes()})
        else:
            if isinstance(view, QTableWidget):
                rows = range(view.rowCount())
            else:
                rows = range(view.model().rowCount())
        if isinstance(view, QTableWidget):
            get = lambda r, c: (view.item(r, c).text() if view.item(r, c) else "")
        else:
            get = lambda r, c: (view.model().index(r, c).data() or "")
        lines = ["\t".join(headers)]
        for r in rows:
            lines.append("\t".join(get(r, c) for c in range(len(headers))))
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText(f"Copied {len(lines) - 1} row(s) to clipboard.")

    def delete_selected(self):
        sel = self.db_view.selectionModel()
        if sel is None or not sel.hasSelection():
            QMessageBox.information(self, APP_NAME,
                                    "Select rows to delete first.")
            return
        ids = sorted({self.db_model.row_id(ix.row())
                      for ix in sel.selectedRows()})
        n = self.qdb.delete(ids)
        self.status.setText(f"Deleted {n} row(s).")
        self.refresh_db()          # totals changed -> reload from the top

    def clear_db(self):
        if self.qdb is None:
            return
        total = self.qdb.count()
        if not total:
            QMessageBox.information(self, APP_NAME,
                                    "The database is already empty.")
            return
        answer = QMessageBox.question(
            self, APP_NAME, f"Delete ALL {total:,} stored questions?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        n = self.qdb.clear()
        self.status.setText(f"Deleted {n} row(s).")
        self.refresh_db()

    def export_db(self):
        if self.qdb is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export all questions", "questions_export.csv",
            "CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        try:
            n = (self.qdb.export_json(path) if path.lower().endswith(".json")
                 else self.qdb.export_csv(path))
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Export failed: {exc}")
            return
        self.status.setText(f"Exported {n:,} rows → {path}")

    # ------------------------------------------------------- dictionary tab --
    def download_dictionary(self):
        source = self.src_combo.currentData()
        if self.store.is_downloaded(source):
            answer = QMessageBox.question(
                self, APP_NAME,
                f"'{source}' is already downloaded.\nRe-download / re-index anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        limit = self.dl_limit.value() or None

        def job(worker):
            self.store._emit = lambda s, d, t: worker.progress.emit(s, d, t)
            self.store._cancel = worker.cancel_event
            self.store.download(source, limit=limit, progress=False,
                                overwrite=False)
            return source
        self._run_worker(job, self._on_downloaded, "Starting download…")

    def _on_downloaded(self, source):
        self.dictionary = self.core.EnglishDictionary(store=self.store)
        self._update_dict_status(f"Active dictionary: {source}")
        self.refresh_db()
        QMessageBox.information(self, APP_NAME,
                                f"Dictionary '{source}' is ready.")

    # ----------------------------------------------------- settings & menus --
    def _load_settings(self):
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self.tabs.setCurrentIndex(self.settings.value("tab", 0, int))
        self.neg_combo.setCurrentIndex(self.settings.value("polarity", 0, int))
        self.chk_fallback.setChecked(self.settings.value("fallback", True, bool))
        self.chk_oov.setChecked(self.settings.value("oov", False, bool))
        self.chk_autosave.setChecked(self.settings.value("autosave", False, bool))
        self.max_kind.setValue(self.settings.value("maxkind", 2, int))
        self.src_combo.setCurrentIndex(self.settings.value("source", 0, int))
        size = self.settings.value("pagesize", "200", str)
        if self.db_pagesize.findText(size) >= 0:
            self.db_pagesize.setCurrentText(size)
        self.act_dark.setChecked(self.settings.value("dark", False, bool))

    def _save_settings(self):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("tab", self.tabs.currentIndex())
        self.settings.setValue("polarity", self.neg_combo.currentIndex())
        self.settings.setValue("fallback", self.chk_fallback.isChecked())
        self.settings.setValue("oov", self.chk_oov.isChecked())
        self.settings.setValue("autosave", self.chk_autosave.isChecked())
        self.settings.setValue("maxkind", self.max_kind.value())
        self.settings.setValue("source", self.src_combo.currentIndex())
        self.settings.setValue("pagesize", self.db_pagesize.currentText())
        self.settings.setValue("dark", self.act_dark.isChecked())

    def set_dark(self, on):
        QApplication.instance().setStyleSheet(DARK if on else STYLE)

    def open_db_folder(self):
        if self.qdb is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.qdb.path)))

    def show_about(self):
        db = self.qdb.path if self.qdb else "(not open)"
        QMessageBox.about(
            self, APP_NAME,
            f"<b>{APP_NAME}</b><br><br>Rule-based yes/no question generation "
            f"with a downloadable English dictionary.<br><br>"
            f"Question database:<br><code>{db}</code><br><br>"
            f"Dictionary cache:<br><code>{os.path.dirname(db)}</code>")

    # ---------------------------------------------------------------- closing --
    def closeEvent(self, ev):
        running = [w for w in self.workers if w.isRunning()]
        if running:
            answer = QMessageBox.question(
                self, APP_NAME,
                "A task is still running (download/index build?). Quit anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            for w in running:
                w.cancel()
                w.terminate()
                w.wait(1500)
        self._save_settings()
        try:
            if self.qdb is not None:
                self.qdb.close()
        except Exception:
            pass
        ev.accept()

# ==================================================================== style ==
STYLE = """
QGroupBox { font-weight: 600; border: 1px solid #d8dee6; border-radius: 8px;
            margin-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                   color: #33415c; }
QPushButton { padding: 6px 14px; border: 1px solid #c6ceda; border-radius: 6px;
              background: #ffffff; }
QPushButton:hover { background: #eef3fb; }
QPushButton:disabled { color: #98a1b3; background: #f2f4f7; }
QPushButton#btnCancel { background: #fdecea; border-color: #c62828; }
QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    border: 1px solid #c6ceda; border-radius: 6px; padding: 4px; }
QTableWidget { alternate-background-color: #f7f9fc; }
"""

DARK = """
QWidget { background-color: #1e2229; color: #d8dee6; }
QGroupBox { border: 1px solid #3a4150; }
QGroupBox::title { color: #9fb3d1; }
QPushButton { background-color: #2a303c; border: 1px solid #3a4150;
              border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background-color: #333b4a; }
QPushButton:disabled { color: #5c6675; background-color: #23272f; }
QPushButton#btnCancel { background-color: #5c2320; border-color: #e5534b; }
QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #23272f; border: 1px solid #3a4150;
    border-radius: 6px; padding: 4px; }
QTableWidget { alternate-background-color: #23272f; background-color: #1e2229;
    gridline-color: #2a303c; }
QTableWidget::item:selected { background-color: #3a4150; }
QTableView { alternate-background-color: #23272f; background-color: #1e2229;
    gridline-color: #2a303c; }
QTableView::item:selected { background-color: #3a4150; }
QHeaderView::section { background-color: #2a303c; border: none; }
QTabWidget::pane { border: 1px solid #3a4150; }
QTabBar::tab { background: #23272f; padding: 6px 12px; }
QTabBar::tab:selected { background: #2a303c; }
QMenuBar, QMenu, QStatusBar { background: #23272f; }
QMenu::item:selected { background: #333b4a; }
QProgressBar { border: 1px solid #3a4150; background: #23272f; }
"""

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()