// DOM and time helpers shared by every view. Server text goes in through textContent only.

const PIDGE_ICON_PATH = "assets/pidge-icon.png";
export const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

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

// One 24-unit icon from one path string or a list of them; styling (fill, stroke) comes from the class.
export function buildSvgIcon(className, pathData) {
  const iconSvg = document.createElementNS(SVG_NAMESPACE, "svg");
  iconSvg.setAttribute("viewBox", "0 0 24 24");
  iconSvg.setAttribute("aria-hidden", "true");
  iconSvg.setAttribute("class", className);
  [].concat(pathData).forEach((singlePathData) => {
    const iconPath = document.createElementNS(SVG_NAMESPACE, "path");
    iconPath.setAttribute("d", singlePathData);
    iconSvg.append(iconPath);
  });
  return iconSvg;
}

export function buildShieldIcon() {
  return buildSvgIcon("shield-icon", "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Z");
}

export function buildTickIcon(className) {
  return buildSvgIcon(className, "M5 12.5 10 17.5 19 7");
}

// "Copy" that puts text on the clipboard for her own mail app; the label span reports the outcome (a blocked clipboard says so),
// so an icon a caller prepends stays.
export function buildCopyButton(textToCopy) {
  const copyButton = createElementWithText("button", "copy-button");
  const copyLabel = createElementWithText("span", "", "Copy");
  copyButton.type = "button";
  copyButton.append(copyLabel);
  copyButton.addEventListener("click", () => {
    if (typeof navigator.clipboard?.writeText !== "function") { copyLabel.textContent = "Select and copy"; return; } // no clipboard API here
    navigator.clipboard.writeText(textToCopy).then(() => { copyLabel.textContent = "Copied"; }, () => { copyLabel.textContent = "Select and copy"; });
  });
  return copyButton;
}
