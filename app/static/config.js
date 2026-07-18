// Runtime deployment routing. Local FastAPI serves same-origin /api routes;
// GitHub Pages calls the separately hosted Render backend.
window.SVE_API_BASE = window.location.hostname.endsWith("github.io")
  ? "https://jamessinghi-securities-valuation-engine-yjua.onrender.com"
  : "";
