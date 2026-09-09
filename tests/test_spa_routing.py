"""Deep links have to survive a refresh.

The app uses `BrowserRouter`, so `/memories` is a real URL in the address bar
that React Router resolves in the browser. Nothing corresponding exists on
disk — the build produces one `index.html` and a bundle — so a *refresh* on
any page other than `/` asks the host for a file it does not have, and the
host answers 404. The app appears to work perfectly until someone reloads,
bookmarks a page, or opens a link you sent them.

The fix is a single rewrite telling Vercel to serve `index.html` for anything
it cannot find on disk, and let the router take it from there. It is one line
of configuration and completely invisible in local development, because
Vite's dev server does this for you — which is exactly why it survives to
production unnoticed.

These are cheap config assertions rather than behavioural tests. There is no
Vercel to run here, but the failure they guard against is silent, remote, and
easy to reintroduce by editing this file for an unrelated reason.
"""

from __future__ import annotations

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config():
    return json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))


class TestTheRewrite:
    def test_a_catch_all_rewrite_exists(self, config):
        assert config.get("rewrites"), (
            "no rewrites: every deep link 404s on refresh, and nothing about "
            "local development will tell you"
        )

    def test_it_catches_every_path(self, config):
        sources = {r.get("source") for r in config["rewrites"]}

        assert "/(.*)" in sources, (
            f"the catch-all is missing; got {sources}. A rewrite that lists "
            f"routes by name goes stale the moment a page is added."
        )

    def test_it_serves_the_app_shell(self, config):
        catch_all = next(r for r in config["rewrites"] if r["source"] == "/(.*)")

        assert catch_all["destination"] == "/index.html"

    def test_the_shell_it_points_at_is_what_the_build_produces(self, config):
        """A rewrite to a file the build does not emit fails the same way as
        no rewrite at all."""
        assert config["outputDirectory"] == "dist"
        assert (ROOT / "index.html").exists(), "no index.html for Vite to build from"


class TestTheRouterThisIsFor:
    def test_the_app_uses_browser_history_not_hashes(self):
        """If this ever became a HashRouter the rewrite would be pointless —
        and if it stays a BrowserRouter the rewrite is mandatory. Worth
        pinning the pair together, since changing one without the other is
        how this breaks."""
        main = (ROOT / "src" / "main.jsx").read_text(encoding="utf-8")

        assert "BrowserRouter" in main

    def test_every_declared_route_is_a_real_deep_link(self):
        """Each of these is a URL someone can refresh on, so each is a URL
        the rewrite has to cover."""
        app = (ROOT / "src" / "App.jsx").read_text(encoding="utf-8")

        assert app.count('<Route path="/') >= 2, "expected several routed pages"
