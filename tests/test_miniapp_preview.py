"""Opt-in real headless Chromium smoke + synthetic PNGs, not real Telegram.

uv run --no-project --with playwright --with aiohttp python tests/test_miniapp_preview.py
No personal browser, credentials, external message sends, or auth bypass in the server.
"""

import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from miniapp import setup
from test_miniapp import signed, TOKEN, TARGET
from aiohttp.test_utils import TestServer


async def main():
    from playwright.async_api import async_playwright

    repo = Path(__file__).resolve().parents[1]
    output = Path("/opt/data/cache/scratch/miniapp-previews")
    output.mkdir(parents=True, exist_ok=True)
    events = []
    errors = []
    console_errors = []
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        service = setup(
            None,
            lambda: TOKEN,
            lambda event: events.append(event) or True,
            state_dir=tmp,
            current_target=lambda: TARGET,
            send_message=lambda _: (_ for _ in ()).throw(
                AssertionError("No sends allowed")
            ),
            env={},
        )
        spec = json.loads((repo / "miniapp/demo.json").read_text())
        rec = service.store.create(spec, "Plan the next release", TARGET, 3600)
        server = TestServer(service.create_app())
        await server.start_server()
        try:
            async with async_playwright() as p:
                installed = list(
                    Path(
                        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent")
                    ).glob("chromium-*/chrome-linux*/chrome")
                )
                browser = await p.chromium.launch(
                    headless=True,
                    executable_path=str(installed[0]) if installed else None,
                    args=["--no-sandbox"],
                )
                for theme in ("light", "dark"):
                    context = await browser.new_context(
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=1,
                        color_scheme=theme,
                    )
                    init = {
                        "initData": signed(rec["id"]),
                        "colorScheme": theme,
                        "themeParams": {},
                    }
                    await context.route(
                        "https://telegram.org/**",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/javascript",
                            body="/* Telegram bridge intentionally stubbed for synthetic local tests. */",
                        ),
                    )
                    await context.add_init_script(
                        "window.Telegram={WebApp:"
                        + json.dumps(init)
                        + '}; Object.assign(Telegram.WebApp,{ready(){},expand(){},onEvent(){}}); window.__violations=[]; document.addEventListener("securitypolicyviolation",e=>window.__violations.push(e.violatedDirective));'
                    )
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "console",
                        lambda msg: (
                            console_errors.append(msg.text)
                            if msg.type == "error"
                            else None
                        ),
                    )
                    await page.goto(str(server.make_url("/m?id=" + rec["id"])))
                    await page.get_by_role(
                        "heading", name="Choose a release approach"
                    ).wait_for()
                    assert await page.locator("#error").is_hidden(), await page.locator(
                        "#error"
                    ).inner_text()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth<=innerWidth"
                    ), "page-level horizontal overflow"
                    assert not await page.evaluate("window.__violations"), (
                        "CSP violations"
                    )
                    await page.screenshot(
                        path=str(output / f"demo-{theme}-390.png"), full_page=True
                    )
                    for slug, name in [
                        ("decision", "Choose a release approach"),
                        ("gantt", "Two-week delivery plan"),
                        ("flow", "Review and release workflow"),
                        ("chart", "Example effort by workstream"),
                        ("form", "Set the pilot preferences"),
                    ]:
                        card = page.get_by_role(
                            "heading", name=name, exact=True
                        ).locator("..")
                        await card.screenshot(
                            path=str(output / f"{slug}-{theme}-390.png")
                        )
                    if theme == "light":
                        # Real DOM choice -> signed HTTP action -> synthetic conversation callback -> persisted state.
                        await page.get_by_role("radio").first.check()
                        await page.get_by_role("button", name="Send my choice").click()
                        await page.get_by_text(
                            "Recorded · Small pilot", exact=True
                        ).wait_for()
                        assert (
                            len(events) == 1 and events[0]["action"]["value"] == "pilot"
                        )
                        await page.reload()
                        await page.get_by_text(
                            "Recorded · Small pilot", exact=True
                        ).wait_for()
                        assert await page.get_by_role(
                            "button", name="Send my choice"
                        ).is_disabled()
                        await page.get_by_label("Pilot name").fill("Preserved draft")
                        draft_update = copy.deepcopy(spec)
                        draft_update["components"].insert(
                            0, {"type": "text", "text": "Draft-preserving update"}
                        )
                        service.store.update(rec["id"], draft_update, TARGET)
                        await page.get_by_text(
                            "Draft-preserving update", exact=True
                        ).wait_for(timeout=8000)
                        assert (
                            await page.get_by_label("Pilot name").input_value()
                            == "Preserved draft"
                        )
                        await page.get_by_label("Pilot name").fill("Synthetic team")
                        await page.get_by_role(
                            "button", name="Send preferences"
                        ).click()
                        await page.wait_for_function(
                            'document.querySelector("[data-action=preferences]").disabled'
                        )
                        for _ in range(100):
                            if len(events) == 2:
                                break
                            await asyncio.sleep(0.02)
                        assert (
                            len(events) == 2
                            and events[1]["action"]["value"]["size"] == 10
                        )
                        # Polling update preserves unsent edits (test separate fresh form).
                        next_spec = copy.deepcopy(spec)
                        next_spec["components"].insert(
                            0, {"type": "text", "text": "Live update received"}
                        )
                        service.store.update(rec["id"], next_spec, TARGET)
                        await page.get_by_text(
                            "Live update received", exact=True
                        ).wait_for(timeout=8000)
                        # XSS string remains literal in the actual browser.
                        xss = copy.deepcopy(next_spec)
                        xss["components"].insert(
                            0,
                            {
                                "type": "text",
                                "text": '<img src=x onerror="window.pwned=1">',
                                "markdown": True,
                            },
                        )
                        service.store.update(rec["id"], xss, TARGET)
                        await page.get_by_text(
                            '<img src=x onerror="window.pwned=1">', exact=True
                        ).wait_for(timeout=8000)
                        assert not await page.evaluate("window.pwned||false")
                        assert await page.locator("#app img").count() == 0
                        service.store.close(rec["id"], TARGET)
                        await page.get_by_text(
                            "Closed · no further submissions", exact=True
                        ).wait_for(timeout=8000)
                        assert (
                            await page.locator(
                                "button[data-action]:not(:disabled)"
                            ).count()
                            == 0
                        )
                        # Fresh fixture for dark previews, no actions already consumed.
                        rec = service.store.create(
                            spec, "Plan the next release", TARGET, 3600
                        )
                    await context.close()
                # Mobile 320 and desktop 1100, including every chart kind and all catalog components.
                from test_miniapp import COMPONENTS

                extra = copy.deepcopy(COMPONENTS)
                for kind in ("line", "hbar", "pie-lite"):
                    extra.append(
                        {
                            "type": "chart",
                            "kind": kind,
                            "labels": ["A", "B"],
                            "series": [{"name": "Values", "values": [1, 2]}],
                        }
                    )
                allrec = service.store.create(
                    {"version": 1, "components": extra}, "All components", TARGET, 3600
                )
                for width in (320, 1100):
                    ctx = await browser.new_context(
                        viewport={"width": width, "height": 900}
                    )
                    await ctx.route(
                        "https://telegram.org/**",
                        lambda route: route.fulfill(status=200, body=""),
                    )
                    await ctx.route(
                        "https://example.com/**",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="image/svg+xml",
                            body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="blue"/></svg>',
                        ),
                    )
                    await ctx.add_init_script(
                        "window.Telegram={WebApp:{initData:"
                        + json.dumps(signed(allrec["id"]))
                        + ',colorScheme:"light",themeParams:{button_color:"#ffffff",button_text_color:"#ffffff"},ready(){},expand(){},onEvent(){}}};'
                    )
                    pg = await ctx.new_page()
                    pg.on("pageerror", lambda error: errors.append(str(error)))
                    await pg.goto(str(server.make_url("/m?id=" + allrec["id"])))
                    await pg.get_by_text("Tab content", exact=True).wait_for()
                    assert await pg.evaluate(
                        "document.documentElement.scrollWidth<=innerWidth"
                    )
                    assert await pg.locator("#app svg").count() == 6
                    await pg.screenshot(
                        path=str(output / f"catalog-light-{width}.png"), full_page=True
                    )
                    await ctx.close()
                # Repeatable controls create new idempotency keys after successful acknowledgement.
                repeat = service.store.create(
                    {
                        "version": 1,
                        "components": [
                            {
                                "type": "buttons",
                                "id": "repeat",
                                "once": False,
                                "actions": [
                                    {
                                        "key": "ping",
                                        "label": "Record again",
                                        "once": False,
                                    }
                                ],
                            }
                        ],
                    },
                    "Repeatability",
                    TARGET,
                    3600,
                )
                ctx = await browser.new_context(viewport={"width": 390, "height": 844})
                await ctx.route(
                    "https://telegram.org/**",
                    lambda route: route.fulfill(status=200, body=""),
                )
                await ctx.add_init_script(
                    "window.Telegram={WebApp:{initData:"
                    + json.dumps(signed(repeat["id"]))
                    + ',colorScheme:"light",themeParams:{},ready(){},expand(){},onEvent(){}}};'
                )
                pg = await ctx.new_page()
                await pg.goto(str(server.make_url("/m?id=" + repeat["id"])))
                baseline = len(events)
                for expected in (baseline + 1, baseline + 2):
                    await pg.get_by_role("button", name="Record again").click()
                    await asyncio.sleep(0.25)
                    assert len(events) == expected, (
                        "repeatable action deduplicated a new click"
                    )
                await ctx.close()
                # A genuinely stalled request must show the 15-second timeout, not a spinner forever.
                ctx = await browser.new_context(viewport={"width": 390, "height": 844})
                await ctx.route(
                    "https://telegram.org/**",
                    lambda route: route.fulfill(status=200, body=""),
                )
                await ctx.route("**/api/spec/**", lambda route: None)
                await ctx.add_init_script(
                    "window.Telegram={WebApp:{initData:"
                    + json.dumps(signed(repeat["id"]))
                    + ",ready(){},expand(){},onEvent(){}}};"
                )
                pg = await ctx.new_page()
                await pg.goto(str(server.make_url("/m?id=" + repeat["id"])))
                await pg.get_by_text(
                    "Request timed out after 15 seconds.", exact=False
                ).wait_for(timeout=18000)
                await pg.screenshot(path=str(output / "timeout-light-390.png"))
                await ctx.close()
                await browser.close()
        finally:
            await server.close()
    assert not errors, errors
    # Image CSP/network noise from external fixtures is separately captured, never hidden as JS PASS.
    report = {
        "passed": True,
        "events_delivered": len(events),
        "page_errors": errors,
        "console_errors": console_errors,
        "previews": [str(x) for x in sorted(output.glob("*.png"))],
        "real_telegram_verified": False,
    }
    (output / "results.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
