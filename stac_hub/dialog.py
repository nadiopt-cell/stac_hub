# -*- coding: utf-8 -*-
"""Главный диалог плагина STAC Hub.

Структура: фильтры поиска сверху, слева дерево «категории/охват -> источники
(-> коллекции)», справа таблица результатов с миниатюрами и провенансом.
Поиск глобальный: один запрос раскладывается по всем отмеченным источникам
в пуле потоков QThreadPool; результаты агрегируются по источникам.
"""

import datetime as _dt
import webbrowser

from qgis.PyQt.QtCore import Qt, QObject, QRunnable, QThreadPool, QSize, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDoubleSpinBox,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import stac_client
from . import render_presets
from .sources import SOURCES, CATEGORIES, COVERAGE_GROUPS, get_source
from .auth_store import AuthStore

COL_THUMB, COL_DATE, COL_SOURCE, COL_COLL, COL_ID, COL_CLOUD, COL_GSD = range(7)

RESULT_ROLE = Qt.UserRole + 1
SOURCE_ROLE = Qt.UserRole + 1
COLL_ROLE = Qt.UserRole + 2


class SearchSignals(QObject):
    done = pyqtSignal(str, list, list)  # source_id, metas, errors


class ThumbSignals(QObject):
    ready = pyqtSignal(object, bytes)  # meta-словарь айтема, данные изображения


class SearchWorker(QRunnable):
    """Поиск по одному источнику (поток из пула QThreadPool)."""

    def __init__(self, dialog, source, bbox, dt_pair, max_cloud, limit, collections=None):
        super().__init__()
        self.dialog = dialog
        self.source = source
        self.bbox = bbox
        self.dt_pair = dt_pair          # ('YYYY-MM-DD', 'YYYY-MM-DD') | (None, None)
        self.max_cloud = max_cloud
        self.limit = limit
        self.collections = collections  # список id коллекций | None
        self.setAutoDelete(True)

    def run(self):
        if self.dialog.cancelled:
            return
        creds = self.dialog.auth_store.get(self.source["id"])
        dt_param = stac_client.datetime_param(*(self.dt_pair or (None, None)))
        try:
            if self.source["kind"] == "static":
                items, warnings = stac_client.walk_static(
                    self.source, self.bbox, self.dt_pair, self.max_cloud, creds,
                    max_items=max(self.limit * 2, 20), max_children=140, depth=4)
                errors = ["{} (предупреждение)".format(w) for w in warnings]
            elif self.source["id"] == "cmrstac":
                checked = set(self.collections or [])
                by_prov = {}
                for c in self.source["collections"]:
                    if not checked or c["id"] in checked:
                        by_prov.setdefault(c["provider"], []).append(c["id"])
                if checked:
                    providers = list(by_prov.keys())
                    cols_by_prov = dict(by_prov)
                else:
                    providers = self.source.get("key_providers") or list(by_prov.keys())
                    cols_by_prov = {p: None for p in providers}
                items, errors = stac_client.search_cmr_providers(
                    self.source, providers, self.bbox, dt_param, cols_by_prov,
                    self.limit, creds)
                if self.max_cloud is not None and self.max_cloud < 100:
                    items = [it for it in items
                             if stac_client.item_cloud(it) is None
                             or stac_client.item_cloud(it) <= self.max_cloud]
                items = items[: self.limit]
            else:
                timeout = 25 if self.source["id"] == "deafrica" else 30
                items, errors = stac_client.search_api(
                    self.source, self.bbox, dt_param, self.collections or None,
                    self.limit, creds, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - любые ошибки сети в фоне
            items, errors = [], [str(exc)]
        # search_api возвращает строку ошибки, search_cmr_providers/walk_static —
        # список; сигнал done(str, list, list) требует список — нормализуем.
        if errors is None:
            errors = []
        elif isinstance(errors, str):
            errors = [errors]
        metas = []
        for it in items:
            try:
                metas.append(stac_client.normalize(it, self.source))
            except Exception:  # noqa: BLE001
                continue
        self.dialog.search_signals.done.emit(self.source["id"], metas, errors)


class ThumbWorker(QRunnable):
    """Загрузка миниатюры (поток, результат через сигнал)."""

    def __init__(self, dialog, meta, url):
        super().__init__()
        self.dialog = dialog
        self.meta = meta
        self.url = url
        self.setAutoDelete(True)

    def run(self):
        if self.dialog.cancelled or not self.url:
            data = b""  # пустой сигнал всё равно освобождает слот очереди
        else:
            # миниатюры MPC лежат в Azure blob — нужна SAS-подпись (иначе 409)
            url = stac_client.sign_url(self.url, (self.meta or {}).get("collection") or "")
            data = stac_client.fetch_thumbnail(url) or b""
        try:
            self.dialog.thumb_signals.ready.emit(self.meta, data)
        except RuntimeError:
            pass  # диалог уже закрыт — слоты уничтожены, тихо выходим


class CredentialsDialog(QDialog):
    """Ввод учётных данных по источникам (Basic / Bearer / произвольный заголовок)."""

    CREDS_UI = [
        ("cmrstac", "NASA CMR-STAC (Earthdata Login)"),
        ("cdse", "Copernicus Data Space"),
        ("terrascope", "Terrascope (VITO)"),
        ("planetarycomputer", "Planetary Computer (ключ API)"),
    ]

    def __init__(self, parent, auth_store):
        super().__init__(parent)
        self.auth_store = auth_store
        self.setWindowTitle("Учётные данные STAC Hub")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Заполняйте только нужные источники. Earthdata Login и аккаунты Copernicus / "
            "Terrascope бесплатны. Без галочки «Запомнить» данные живут до закрытия QGIS. "
            "Хранение в QSettings не шифруется.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        for i, (sid, caption) in enumerate(self.CREDS_UI):
            src = get_source(sid)
            box = QGroupBox(caption)
            inner = QVBoxLayout(box)
            note = src.get("auth_note", "")
            if note:
                nl = QLabel(note)
                nl.setWordWrap(True)
                inner.addWidget(nl)
            inner.addWidget(self._fields(sid))
            grid.addWidget(box, i // 2, i % 2)
        lay.addLayout(grid)
        btns = QHBoxLayout()
        btn_save = QPushButton("Сохранить")
        btn_save.clicked.connect(self.accept)
        btn_clear = QPushButton("Очистить всё")
        btn_clear.clicked.connect(self._clear_all)
        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_save)
        btns.addWidget(btn_clear)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        lay.addLayout(btns)

    def _fields(self, sid):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        combo.addItems(["Без авторизации", "Basic (логин/пароль)", "Bearer-токен", "Заголовок"])
        lay.addWidget(combo)
        user = QLineEdit()
        user.setPlaceholderText("логин")
        pwd = QLineEdit()
        pwd.setPlaceholderText("пароль")
        pwd.setEchoMode(QLineEdit.Password)
        tok = QLineEdit()
        tok.setPlaceholderText("токен / ключ")
        tok.setEchoMode(QLineEdit.Password)
        hname = QLineEdit()
        hname.setPlaceholderText("имя заголовка (напр. Ocp-Apim-Subscription-Key)")
        hval = QLineEdit()
        hval.setPlaceholderText("значение заголовка")
        for x in (user, pwd, tok, hname, hval):
            lay.addWidget(x)
        remember = QCheckBox("Запомнить (сохранить в настройках QGIS)")
        lay.addWidget(remember)

        def sync_mode():
            mode = combo.currentIndex()
            user.setVisible(mode == 1)
            pwd.setVisible(mode == 1)
            tok.setVisible(mode == 2)
            hname.setVisible(mode == 3)
            hval.setVisible(mode == 3)

        combo.currentIndexChanged.connect(sync_mode)
        creds = self.auth_store.get(sid)
        if creds:
            combo.setCurrentIndex({"basic": 1, "bearer": 2, "header": 3}.get(creds.get("mode"), 0))
            user.setText(creds.get("user", ""))
            tok.setText(creds.get("token", ""))
            hname.setText(creds.get("header_name", ""))
            hval.setText(creds.get("header_value", ""))
            remember.setChecked(True)
        sync_mode()
        self.rows[sid] = {"combo": combo, "user": user, "pwd": pwd, "token": tok,
                          "hname": hname, "hval": hval, "remember": remember}
        return w

    rows = {}

    def accept(self):
        for sid, r in self.rows.items():
            mode = {0: "none", 1: "basic", 2: "bearer", 3: "header"}[r["combo"].currentIndex()]
            creds = {
                "mode": mode, "user": r["user"].text().strip(),
                "password": r["pwd"].text(), "token": r["token"].text().strip(),
                "header_name": r["hname"].text().strip(),
                "header_value": r["hval"].text().strip(),
            }
            self.auth_store.set(sid, creds, remember=r["remember"].isChecked())
        super().accept()

    def _clear_all(self):
        for sid, r in self.rows.items():
            r["combo"].setCurrentIndex(0)
            r["remember"].setChecked(False)
            self.auth_store.clear(sid)


class StacHubDialog(QDialog):
    """Главное окно плагина."""

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.iface = plugin.iface
        self.auth_store = AuthStore()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(8)
        self.cancelled = False
        self._pending = 0
        self._thumb_items = {}
        self._thumb_queue = []      # очередь миниатюр: сглаживает нагрузку на сеть/GIL
        self._thumb_active = 0
        self.THUMB_PARALLEL = 2
        # общие долгоживущие сигналы: владельцем является диалог —
        # объекты не умирают вместе с autoDelete-раннерблами
        self._workers = []
        self.search_signals = SearchSignals()
        self.search_signals.done.connect(self._search_done)
        self.thumb_signals = ThumbSignals()
        self.thumb_signals.ready.connect(self.thumb_ready)

        self.setWindowTitle("STAC Hub — открытые геоданные")
        self.resize(1180, 720)
        self.setModal(False)

        root = QVBoxLayout(self)

        # ----------------------------------------------------- фильтры
        filters = QGroupBox("Поиск по всем отмеченным источникам")
        grid = QVBoxLayout(filters)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Группировка:"))
        self.group_mode = QComboBox()
        self.group_mode.addItems(["По категориям", "По охвату"])
        self.group_mode.currentIndexChanged.connect(self.rebuild_tree)
        row1.addWidget(self.group_mode)
        row1.addWidget(QLabel("Фильтр охвата:"))
        self.coverage_filter = QComboBox()
        self.coverage_filter.addItems(["Все"] + [label for _, label in COVERAGE_GROUPS])
        self.coverage_filter.currentIndexChanged.connect(self.rebuild_tree)
        row1.addWidget(self.coverage_filter)
        row1.addStretch(1)
        btn_all = QPushButton("Отметить все")
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none = QPushButton("Снять все")
        btn_none.clicked.connect(lambda: self._check_all(False))
        row1.addWidget(btn_all)
        row1.addWidget(btn_none)
        grid.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("BBox (д, ю, в, с):"))
        self.spins = []
        for label, lo, hi in [("д", -180.0, 180.0), ("ю", -90.0, 90.0),
                              ("в", -180.0, 180.0), ("с", -90.0, 90.0)]:
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setDecimals(4)
            sp.setSingleStep(0.5)
            sp.setValue(lo if label in ("д", "ю") else hi)
            self.spins.append(sp)
            row2.addWidget(sp)
        btn_extent = QPushButton("Взять экстент карты")
        btn_extent.clicked.connect(self.use_canvas_extent)
        row2.addWidget(btn_extent)
        btn_world = QPushButton("Весь мир")
        btn_world.clicked.connect(self.clear_bbox)
        row2.addWidget(btn_world)
        grid.addLayout(row2)

        row3 = QHBoxLayout()
        self.chk_from = QCheckBox("с")
        self.chk_from.setToolTip("Ограничение даты съёмки «с» (включительно)")
        self.date_from = QDateTimeEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("dd.MM.yyyy")
        self.date_from.setDateTime(_dt.datetime(2020, 1, 1))
        self.chk_to = QCheckBox("по")
        self.date_to = QDateTimeEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("dd.MM.yyyy")
        self.date_to.setDateTime(_dt.datetime.now())
        for w in (self.chk_from, self.date_from, self.chk_to, self.date_to):
            row3.addWidget(w)
        row3.addWidget(QLabel("Облачность ≤"))
        self.cloud = QSpinBox()
        self.cloud.setRange(0, 100)
        self.cloud.setValue(100)
        self.cloud.setSuffix(" %")
        self.cloud.setToolTip("100 = без ограничения по облачности")
        row3.addWidget(self.cloud)
        row3.addWidget(QLabel("Лимит/источник"))
        self.limit = QSpinBox()
        self.limit.setRange(5, 200)
        self.limit.setValue(15)
        row3.addWidget(self.limit)
        row3.addStretch(1)
        self.btn_creds = QPushButton("Учётные данные…")
        self.btn_creds.clicked.connect(self.open_credentials)
        self.btn_stop = QPushButton("Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_search)
        self.btn_search = QPushButton("Найти")
        self.btn_search.setDefault(True)
        self.btn_search.clicked.connect(self.run_search)
        for w in (self.btn_creds, self.btn_search, self.btn_stop):
            row3.addWidget(w)
        grid.addLayout(row3)
        root.addWidget(filters)

        # --------------------------------------------------- сплиттер
        split = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Категории и источники"])
        self.tree.setColumnWidth(0, 360)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_source_selected)
        split.addWidget(self.tree)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Фото", "Дата", "Источник (оператор)", "Коллекция",
                                      "ID", "Обл.", "GSD"])
        self.results.setIconSize(QSize(64, 64))
        self.results.setUniformRowHeights(True)
        self.results.itemDoubleClicked.connect(self.add_selected_to_project)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._results_menu)
        self.results.itemSelectionChanged.connect(self._on_result_selected)
        rlay.addWidget(self.results)
        # строка выбора отображения: RGB-синтезы и спектральные индексы
        rend = QHBoxLayout()
        rend.addWidget(QLabel("Отображение:"))
        self.render_combo = QComboBox()
        self.render_combo.setToolTip(
            "RGB-синтезы и спектральные индексы для выбранного снимка. "
            "Индексы считаются по текущему экстенту карты.")
        rend.addWidget(self.render_combo, 1)
        self.btn_add = QPushButton("Добавить слой")
        self.btn_add.clicked.connect(self.add_selected_to_project)
        rend.addWidget(self.btn_add)
        rlay.addLayout(rend)
        self.info = QLabel("Выберите источник слева — здесь появятся сведения о происхождении данных.")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#444; padding:4px;")
        self.info.setOpenExternalLinks(True)
        rlay.addWidget(self.info)
        split.addWidget(right)
        split.setSizes([420, 760])
        root.addWidget(split, 1)

        # ------------------------------------------------------ статус
        srow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(160)
        self.status = QLabel("Готово. Отметьте источники и нажмите «Найти».")
        srow.addWidget(self.progress)
        srow.addWidget(self.status, 1)
        root.addLayout(srow)

        self.rebuild_tree()

    # ------------------------------------------------------------ дерево
    def rebuild_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        by_coverage = self.group_mode.currentIndex() == 1
        cov_idx = max(self.coverage_filter.currentIndex() - 1, -1)

        groups = COVERAGE_GROUPS if by_coverage else CATEGORIES
        for gkey, glabel in groups:
            if by_coverage and cov_idx >= 0 and gkey != COVERAGE_GROUPS[cov_idx][0]:
                continue
            top = QTreeWidgetItem([glabel])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            for src in SOURCES:
                if by_coverage:
                    match = src["coverage_class"] == gkey
                    cat_items = []
                else:
                    cat_items = [c for c in src["collections"] if c["category"] == gkey]
                    match = bool(cat_items) or gkey in src.get("categories", [])
                if not match:
                    continue
                lock = " [вход]" if src.get("auth") not in (None, "none") else ""
                snode = QTreeWidgetItem([src["name"] + lock])
                snode.setData(0, SOURCE_ROLE, src["id"])
                snode.setFlags(snode.flags() | Qt.ItemIsUserCheckable)
                snode.setCheckState(0, Qt.Checked)
                snode.setToolTip(0, self._source_tooltip(src))
                top.addChild(snode)
                for c in cat_items:
                    cnode = QTreeWidgetItem(["    · " + (c.get("name") or c["id"])])
                    cnode.setData(0, COLL_ROLE, (src["id"], c["id"]))
                    cnode.setFlags(cnode.flags() | Qt.ItemIsUserCheckable)
                    cnode.setCheckState(0, Qt.Unchecked)
                    cnode.setToolTip(0, "{}\nСпутник: {}\nРазрешение: {}\nФормат: {}\nДоступ: {}\n{}".format(
                        c["id"], c.get("sat", "—"), c.get("gsd", "—"),
                        c.get("format", "—"), c.get("access", "—"), c.get("note", "")))
                    snode.addChild(cnode)
            self.tree.expandItem(top)
        self.tree.blockSignals(False)

    @staticmethod
    def _source_tooltip(src):
        return ("Оператор: {}\nКаталог STAC: {}\nДоступ: {}\nЛицензия: {}\nОхват: {}\n\n{}".format(
            src["operator"], src["url"], src["access"], src.get("license", "—"),
            src["coverage"], src.get("note", "")))

    def _check_all(self, state):
        def walk(item):
            if item.data(0, SOURCE_ROLE):
                item.setCheckState(0, Qt.Checked if state else Qt.Unchecked)
                if not state:
                    for i in range(item.childCount()):
                        item.child(i).setCheckState(0, Qt.Unchecked)
            for i in range(item.childCount()):
                walk(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def _on_item_changed(self, item, column):
        if item.data(0, SOURCE_ROLE) and item.checkState(0) == Qt.Unchecked:
            for i in range(item.childCount()):
                item.child(i).setCheckState(0, Qt.Unchecked)

    def _on_source_selected(self):
        sel = self.tree.selectedItems()
        if not sel:
            return
        node = sel[0]
        sid = node.data(0, SOURCE_ROLE)
        if not sid and node.parent() is not None:
            sid = node.parent().data(0, SOURCE_ROLE)
        src = get_source(sid) if sid else None
        if src:
            auth_html = "<br/>" + src["auth_note"] if src.get("auth_note") else ""
            self.info.setText(
                "<b>{1}</b> — оператор: <b>{2}</b><br/>"
                "Каталог STAC: <a href='{0}'>{0}</a><br/>"
                "Доступ: {3} · Лицензия: {4} · Охват: {5}{6}".format(
                    src["url"], src["name"], src["operator"],
                    src["access"], src.get("license", "—"), src["coverage"], auth_html))

    # ------------------------------------------------------------ поиск
    def run_search(self):
        targets = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            seen = set()
            for j in range(top.childCount()):
                snode = top.child(j)
                sid = snode.data(0, SOURCE_ROLE)
                if sid in seen or snode.checkState(0) != Qt.Checked:
                    continue
                seen.add(sid)
                cols = [snode.child(k).data(0, COLL_ROLE)[1]
                        for k in range(snode.childCount())
                        if snode.child(k).checkState(0) == Qt.Checked]
                targets.append((get_source(sid), cols))
        if not targets:
            self.status.setText("Отметьте хотя бы один источник в дереве слева.")
            return
        self.cancelled = False
        self.btn_stop.setEnabled(True)
        self.btn_search.setEnabled(False)
        self.results.clear()
        self._thumb_items.clear()
        bbox = self.current_bbox()
        dt_from = self.date_from.date().toString("yyyy-MM-dd") if self.chk_from.isChecked() else None
        dt_to = self.date_to.date().toString("yyyy-MM-dd") if self.chk_to.isChecked() else None
        cloud = None if self.cloud.value() >= 100 else self.cloud.value()
        limit = self.limit.value()
        self.progress.setRange(0, len(targets))
        self.progress.setValue(0)
        self._pending = len(targets)
        self.status.setText("Поиск в {} источниках…".format(len(targets)))
        self._workers = []  # python-ссылки, чтобы GC не убивал обёртки до выполнения
        for src, cols in targets:
            worker = SearchWorker(self, src, bbox, (dt_from, dt_to), cloud, limit,
                                  collections=cols or None)
            self._workers.append(worker)
            self.pool.start(worker)

    def stop_search(self):
        self.cancelled = True
        self.pool.clear()
        self.btn_stop.setEnabled(False)
        self.btn_search.setEnabled(True)
        self.status.setText("Остановлено.")

    def _search_done(self, source_id, metas, errors):
        src = get_source(source_id)
        self._pending = max(self._pending - 1, 0)
        self.progress.setValue(self.progress.maximum() - self._pending)
        if metas:
            top = QTreeWidgetItem(["{} — {} (найдено: {})".format(
                src["name"], src["operator"], len(metas))])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setToolTip(0, self._source_tooltip(src))
            for meta in metas:
                node = QTreeWidgetItem([
                    "", meta["date"], meta["source_name"], meta["collection"] or "—",
                    meta["item_id"],
                    "{:.0f}".format(meta["cloud"]) if meta["cloud"] is not None else "—",
                    str(meta["gsd"]) if meta["gsd"] is not None else "—",
                ])
                node.setData(0, RESULT_ROLE, meta)
                node.setToolTip(COL_ID, meta["item_id"])
                top.addChild(node)
                if meta["thumb_url"]:
                    self._thumb_items[id(meta)] = (node, meta)
                    self._thumb_queue.append((meta, meta["thumb_url"]))
                    self._pump_thumbs()
            self.results.addTopLevelItem(top)
        if errors:
            self.status.setText("{}: {}".format(src["name"], "; ".join(errors)[:180]))
        if self._pending == 0:
            self.btn_stop.setEnabled(False)
            self.btn_search.setEnabled(True)
            self.progress.setRange(0, 1)
            groups = self.results.topLevelItemCount()
            total = sum(self.results.topLevelItem(i).childCount()
                        for i in range(groups))
            self.status.setText(
                "Готово: {} наборов в {} источниках. Двойной клик по снимку — добавить слой в проект.".format(
                    total, groups))

    def _pump_thumbs(self):
        """Запускает загрузку миниатюр с ограничением параллелизма."""
        while (self._thumb_queue and self._thumb_active < self.THUMB_PARALLEL
               and not self.cancelled):
            meta, url = self._thumb_queue.pop(0)
            self._thumb_active += 1
            tw = ThumbWorker(self, meta, url)
            self._workers.append(tw)
            self.pool.start(tw)

    def thumb_ready(self, meta, data):
        self._thumb_active = max(self._thumb_active - 1, 0)
        entry = self._thumb_items.get(id(meta))
        if entry and data:
            node, _meta = entry
            pix = QPixmap()
            if pix.loadFromData(data):
                node.setIcon(COL_THUMB,
                             QIcon(pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        self._pump_thumbs()

    # -------------------------------------------------------- результаты
    def _selected_meta(self):
        sel = self.results.selectedItems()
        if not sel:
            return None
        return sel[0].data(0, RESULT_ROLE)

    def _on_result_selected(self):
        meta = self._selected_meta()
        if not meta:
            return
        asset = meta.get("asset_url", "")
        self.info.setText(
            "<b>{}</b> · {}<br/>"
            "Оператор: <b>{}</b> · Лицензия: {} · Доступ: {}<br/>"
            "Ассет [{}]: {}<br/>"
            "<a href='{}'>STAC-айтем</a>{}".format(
                meta["item_id"], meta["collection"], meta["operator"],
                meta.get("license", "—"), meta.get("access", "—"),
                meta.get("asset_mime") or "—", asset[:110],
                meta.get("self_url") or (asset or "#"),
                "" if not asset else " · <a href='{}'>COG-ссылка</a>".format(asset)))
        # доступные для этого снимка синтезы/индексы
        self.render_combo.blockSignals(True)
        self.render_combo.clear()
        for key, label in render_presets.menu_options(meta.get("assets") or {}):
            self.render_combo.addItem(label, key)
        self.render_combo.setCurrentIndex(0)
        self.render_combo.blockSignals(False)

    def _results_menu(self, pos):
        meta = self._selected_meta()
        if not meta:
            return
        menu = QMenu(self)
        act_add = menu.addAction("Добавить слой в проект")
        act_copy = menu.addAction("Копировать ссылку COG")
        act_open = menu.addAction("Открыть STAC-айтем в браузере")
        act = menu.exec_(self.results.viewport().mapToGlobal(pos))
        if act == act_add:
            self.add_selected_to_project()
        elif act == act_copy:
            QApplication.clipboard().setText(meta.get("asset_url", ""))
            self.status.setText("Ссылка скопирована: {}".format(meta.get("asset_url", "")[:100]))
        elif act == act_open:
            url = meta.get("self_url") or meta.get("asset_url")
            if url:
                webbrowser.open(url)

    def add_selected_to_project(self):
        meta = self._selected_meta()
        if not meta:
            return
        if not meta.get("asset_url"):
            QMessageBox.warning(self, "STAC Hub",
                                "У айтема нет доступного растрового ассета (только метаданные).")
            return
        name = "{} · {}".format(meta["collection"] or meta["source_name"], meta["date"])
        creds = self.auth_store.get(meta["source_id"])
        preset = self.render_combo.currentData() or "plain"
        self.status.setText("Строим слой ({})…".format(self.render_combo.currentText()))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            fn = getattr(self.plugin, "add_rendered_layer", None)
            if fn is not None:
                ok, msg = fn(meta, preset, creds)
            else:  # упрощённая заглушка (тесты) — ассет как есть
                ok, msg = self.plugin.add_cog_layer(meta["asset_url"], name, creds)
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            self.status.setText("Слой добавлен: {}".format(msg or name))
        else:
            self.status.setText("Не удалось добавить слой. {}".format(msg))

    # ------------------------------------------------------------ bbox
    def current_bbox(self):
        w, s, e, n = [sp.value() for sp in self.spins]
        if (w, s, e, n) == (-180.0, -90.0, 180.0, 90.0):
            return None  # весь мир — фильтр не нужен
        return [w, s, e, n]

    def use_canvas_extent(self):
        bbox = self.plugin.canvas_bbox()
        if not bbox:
            self.status.setText("Нет доступа к карте.")
            return
        for sp, val in zip(self.spins, bbox):
            sp.setValue(val)

    def clear_bbox(self):
        for sp, val in zip(self.spins, (-180.0, -90.0, 180.0, 90.0)):
            sp.setValue(val)

    # ------------------------------------------------------------ прочее
    def open_credentials(self):
        dlg = CredentialsDialog(self, self.auth_store)
        dlg.exec_()
