(function () {
  function updateLanguageLinks() {
    const root = "/ChangePilot/";
    const englishRoot = `${root}en/`;
    const path = window.location.pathname;
    const relativePath = path.startsWith(englishRoot)
      ? path.slice(englishRoot.length)
      : path.startsWith(root)
        ? path.slice(root.length)
        : "";

    const targets = {
      zh: `${root}${relativePath}`,
      en: `${englishRoot}${relativePath}`,
    };

    for (const [language, href] of Object.entries(targets)) {
      document
        .querySelectorAll(`[hreflang="${language}"]`)
        .forEach((element) => element.setAttribute("href", href));
    }
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(updateLanguageLinks);
  } else {
    document.addEventListener("DOMContentLoaded", updateLanguageLinks);
  }
})();
