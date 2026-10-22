// DOM and time helpers shared by every view. Server text goes in through textContent only.

const PIDGE_ICON_PATH = "assets/pidge-icon.png";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

export function createElementWithText(tagName, className, textContent) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (textContent !== undefined) {
    element.textContent = textContent;
  }
  return element;
}

// Returns null when the text is not a readable timestamp; callers decide how to report it.
export function parseTimestampMilliseconds(isoText) {
  const timestampMilliseconds = Date.parse(isoText);
  return Number.isNaN(timestampMilliseconds) ? null : timestampMilliseconds;
}

export function formatRemainingTimeAsMinutesSeconds(remainingMilliseconds) {
  const totalSeconds = Math.max(0, Math.ceil(remainingMilliseconds / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export function buildPidgeIconElement(className, alternativeText) {
  const pidgeIcon = createElementWithText("img", className);
  pidgeIcon.src = PIDGE_ICON_PATH;
  pidgeIcon.alt = alternativeText;
  return pidgeIcon;
}

function buildSvgIcon(className, pathData) {
  const iconSvg = document.createElementNS(SVG_NAMESPACE, "svg");
  iconSvg.setAttribute("viewBox", "0 0 24 24");
  iconSvg.setAttribute("aria-hidden", "true");
  iconSvg.setAttribute("class", className);
  const iconPath = document.createElementNS(SVG_NAMESPACE, "path");
  iconPath.setAttribute("d", pathData);
  iconSvg.append(iconPath);
  return iconSvg;
}

export function buildShieldIcon() {
  return buildSvgIcon("shield-icon", "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Z");
}

export function buildTickIcon(className) {
  return buildSvgIcon(className, "M5 12.5 10 17.5 19 7");
}
