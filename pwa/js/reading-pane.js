// Gmail's reading pane on a wide screen: No split (default), Vertical split or Horizontal split, picked from the small menu in
// the inbox tools row and remembered per browser. Under WIDE_SCREEN_MIN_WIDTH_PX (phones) the inbox always reads with no split.
import { READING_PANE_LAYOUT } from "./constants.js";
import { buildSvgIcon, createElementWithText } from "./dom-helpers.js";

const WIDE_SCREEN_MIN_WIDTH_PX = 900;
const LAYOUT_STORAGE_KEY = "sendpidge.readingPaneLayout";
const KNOWN_LAYOUTS = new Set(Object.values(READING_PANE_LAYOUT));
const LAYOUT_LABELS = Object.freeze({
  [READING_PANE_LAYOUT.NO_SPLIT]: "No split",
  [READING_PANE_LAYOUT.VERTICAL_SPLIT]: "Vertical split",
  [READING_PANE_LAYOUT.HORIZONTAL_SPLIT]: "Horizontal split",
});
const PANE_FRAME_PATH = "M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z";
const LAYOUT_ICON_PATHS = Object.freeze({
  [READING_PANE_LAYOUT.NO_SPLIT]: [PANE_FRAME_PATH],
  [READING_PANE_LAYOUT.VERTICAL_SPLIT]: [PANE_FRAME_PATH, "M10 4v16"],
  [READING_PANE_LAYOUT.HORIZONTAL_SPLIT]: [PANE_FRAME_PATH, "M3 11h18"],
});
const CARET_PATH = "M7 10l5 5 5-5z";
const ARROW_LEFT_PATH = "M19 12H5M12 19l-7-7 7-7";
const CHEVRON_LEFT_PATH = "M15 5 8 12l7 7";
const CHEVRON_RIGHT_PATH = "M9 5l7 7-7 7";

const wideScreenQuery = window.matchMedia(`(min-width: ${WIDE_SCREEN_MIN_WIDTH_PX}px)`);
let activeLayout = readStoredLayout();
let isMenuOpen = false;
let onLayoutChanged = () => {};

const pageElements = {
  readingPane: document.getElementById("reading-pane"),
  paneTools: document.getElementById("pane-tools"),
  readerBar: document.getElementById("reader-bar"),
};

function readStoredLayout() {
  try {
    const storedLayout = window.localStorage.getItem(LAYOUT_STORAGE_KEY);
    return KNOWN_LAYOUTS.has(storedLayout) ? storedLayout : READING_PANE_LAYOUT.NO_SPLIT;
  } catch (storageError) {
    console.error("SendPidge: could not read the reading-pane choice", storageError.name);
    return READING_PANE_LAYOUT.NO_SPLIT;
  }
}

function storeLayout(layout) {
  try {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, layout);
  } catch (storageError) {
    console.error("SendPidge: could not save the reading-pane choice; it lasts for this visit only", storageError.name);
  }
}

export function isSplitReadingPane() {
  return wideScreenQuery.matches && activeLayout !== READING_PANE_LAYOUT.NO_SPLIT;
}

function applyLayout() {
  pageElements.readingPane.dataset.layout = wideScreenQuery.matches ? activeLayout : READING_PANE_LAYOUT.NO_SPLIT;
  onLayoutChanged();
}

function handleUserPickedLayout(layout) {
  activeLayout = layout;
  isMenuOpen = false;
  storeLayout(layout);
  applyLayout();
}

function buildIconButton(className, pathDataList, labelText, onClick) {
  const button = createElementWithText("button", `pane-icon-button ${className}`);
  button.type = "button";
  button.setAttribute("aria-label", labelText);
  button.title = labelText;
  button.append(buildSvgIcon("pane-icon", pathDataList));
  button.addEventListener("click", onClick);
  return button;
}

function buildLayoutMenuElement() {
  const menu = createElementWithText("div", "pane-menu");
  menu.setAttribute("role", "menu");
  menu.hidden = !isMenuOpen;
  Object.values(READING_PANE_LAYOUT).forEach((layout) => {
    const item = createElementWithText("button", "pane-menu-item");
    item.type = "button";
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", String(layout === activeLayout));
    item.append(buildSvgIcon("pane-icon", LAYOUT_ICON_PATHS[layout]), createElementWithText("span", "", LAYOUT_LABELS[layout]));
    item.addEventListener("click", (clickEvent) => { clickEvent.stopPropagation(); handleUserPickedLayout(layout); });
    menu.append(item);
  });
  return menu;
}

// The inbox tools row: "1–N of N" and the layout menu; empty on a phone.
export function renderPaneTools(rowCount) {
  if (!wideScreenQuery.matches) {
    pageElements.paneTools.replaceChildren();
    return;
  }
  const menuButton = buildIconButton("pane-menu-button", LAYOUT_ICON_PATHS[activeLayout], "Reading pane", (clickEvent) => {
    clickEvent.stopPropagation();
    isMenuOpen = !isMenuOpen;
    renderPaneTools(rowCount);
  });
  menuButton.append(buildSvgIcon("pane-caret", CARET_PATH));
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.setAttribute("aria-expanded", String(isMenuOpen));
  const menuWrap = createElementWithText("span", "pane-menu-wrap");
  menuWrap.append(menuButton, buildLayoutMenuElement());
  pageElements.paneTools.replaceChildren(createElementWithText("span", "pane-count", `1–${rowCount} of ${rowCount}`), menuWrap);
}

// Gmail's reader bar above an open email in No split on a wide screen: back, "n of N", older and newer.
export function renderReaderBar({ emailIds, openEmailId, onBack, onOpen }) {
  const isShown = wideScreenQuery.matches && activeLayout === READING_PANE_LAYOUT.NO_SPLIT;
  pageElements.readerBar.hidden = !isShown;
  if (!isShown) {
    pageElements.readerBar.replaceChildren();
    return;
  }
  const openIndex = emailIds.indexOf(openEmailId);
  const newerButton = buildIconButton("", [CHEVRON_LEFT_PATH], "Newer", () => onOpen(emailIds[openIndex - 1]));
  newerButton.disabled = openIndex <= 0;
  const olderButton = buildIconButton("", [CHEVRON_RIGHT_PATH], "Older", () => onOpen(emailIds[openIndex + 1]));
  olderButton.disabled = openIndex < 0 || openIndex >= emailIds.length - 1;
  pageElements.readerBar.replaceChildren(
    buildIconButton("", [ARROW_LEFT_PATH], "Back to Inbox", onBack),
    createElementWithText("span", "pane-grow"),
    createElementWithText("span", "pane-count", `${openIndex + 1} of ${emailIds.length}`),
    newerButton, olderButton,
  );
}

// onChanged runs whenever the effective layout changes (menu pick or the window crossing the wide-screen width).
export function startReadingPane(onChanged) {
  onLayoutChanged = onChanged;
  wideScreenQuery.addEventListener("change", applyLayout);
  document.addEventListener("click", () => {
    if (!isMenuOpen) return;
    isMenuOpen = false;
    const menu = pageElements.paneTools.querySelector(".pane-menu");
    if (menu !== null) menu.hidden = true;
  });
  pageElements.readingPane.dataset.layout = wideScreenQuery.matches ? activeLayout : READING_PANE_LAYOUT.NO_SPLIT;
}
