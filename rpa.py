import asyncio
from pathlib import Path

import yaml
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

USER_DATA_DIR = Path(__file__).parent / ".camoufox-profile"
CONFIG_PATH = Path(__file__).parent / "config.yaml"
REMOVED_LOG_PATH = Path(__file__).parent / "removed_members.log"

ADMIN_MARKER_SELECTOR = (
    '[data-testid="community-creator-marker"], [data-testid="community-admin-marker"]'
)
# Delay between removals so WhatsApp doesn't flag the account for bulk actions.
REMOVE_DELAY_SECONDS = 3


class AsyncCamoufoxClient:
    async def run(self) -> None:
        config = yaml.safe_load(CONFIG_PATH.read_text())
        search_term = config["search"]

        async with AsyncCamoufox(
            headless=False,
            persistent_context=True,
            user_data_dir=str(USER_DATA_DIR),
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://web.whatsapp.com/")

            await self._wait_for_login(page)
            members_dialog = await self._open_members_dialog(page, search_term)
            await self._remove_non_admins(page, members_dialog)

            print("Browser stays open. Close it or press Ctrl+C to stop.")
            try:
                await context.wait_for_event("close", timeout=0)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    async def _wait_for_login(self, page: Page) -> None:
        qr_code = page.locator('[data-testid="link-device-qr-code"]')
        try:
            # If the profile is already logged in, WhatsApp never renders
            # the QR canvas, so this just times out instead of matching.
            await qr_code.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError:
            print("Already logged in.")
            return

        print("Scan the QR code to log in...")
        # WhatsApp periodically swaps the QR canvas for a fresh one (same
        # selector, new node), which also fires "hidden" - so a hidden QR
        # isn't proof of login until it stays gone.
        while True:
            await qr_code.wait_for(state="hidden", timeout=0)
            await asyncio.sleep(2)
            qr_code = page.locator('[data-testid="link-device-qr-code"]')
            if await qr_code.count() == 0:
                break
        print("Logged in.")

    async def _open_members_dialog(self, page: Page, search_term: str) -> Locator:
        search_box = page.get_by_role("textbox", name="Search or start a new chat")
        await search_box.wait_for(state="visible", timeout=15000)
        await search_box.fill(search_term)
        await page.wait_for_timeout(2000)

        result = page.get_by_title(search_term, exact=True).first
        await result.wait_for(state="visible", timeout=10000)
        await result.click()
        await page.wait_for_timeout(1500)

        manage = page.get_by_text("Manage community", exact=True)
        await manage.wait_for(state="visible", timeout=10000)
        await manage.click()
        await page.wait_for_timeout(1500)

        # Clicking the "N community members" header opens a dedicated,
        # unvirtualized-by-clutter "Members (N)" dialog - easier to scroll
        # and scope queries into than the mixed community-info sidebar.
        members_header = page.get_by_text("community members", exact=False).first
        await members_header.wait_for(state="visible", timeout=10000)
        await members_header.click()

        members_dialog = page.get_by_role("dialog", name="Members", exact=False)
        await members_dialog.wait_for(state="visible", timeout=10000)
        return members_dialog

    async def _list_non_admin_members(self, page: Page, scope: Locator) -> list[str]:
        """Scrolls the (virtualized) member list and collects every name
        that has no owner/admin marker."""
        names: dict[str, bool] = {}
        stagnant_rounds = 0
        last_count = -1
        await scope.hover()
        while stagnant_rounds < 3:
            for row in await scope.locator('[data-testid="cell-frame-container"]').all():
                title_el = row.locator('[data-testid="cell-frame-title"] span[title]').first
                try:
                    name = await title_el.get_attribute("title", timeout=1000)
                except PlaywrightTimeoutError:
                    continue
                if not name:
                    continue
                is_admin = await row.locator(ADMIN_MARKER_SELECTOR).count() > 0
                names[name] = names.get(name, False) or is_admin

            if len(names) == last_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            last_count = len(names)
            await page.mouse.wheel(0, 400)
            await page.wait_for_timeout(500)

        return [name for name, is_admin in names.items() if not is_admin]

    async def _remove_non_admins(self, page: Page, scope: Locator) -> None:
        targets = await self._list_non_admin_members(page, scope)
        if not targets:
            print("No non-admin members found.")
            return

        print(f"About to remove {len(targets)} non-admin member(s): {', '.join(targets)}")
        answer = await asyncio.to_thread(input, "Type 'yes' to confirm removal: ")
        if answer.strip().lower() != "yes":
            print("Aborted, no one was removed.")
            return

        removed = []
        for name in targets:
            try:
                row = scope.get_by_text(name, exact=True).first
                await row.scroll_into_view_if_needed(timeout=5000)
                await row.hover()

                ctx_btn = scope.locator('[data-testid="context-btn"]').first
                await ctx_btn.wait_for(state="visible", timeout=5000)
                await ctx_btn.click()

                # The context menu and confirm dialog render as page-level
                # overlays, outside the members dialog's own DOM subtree.
                remove_item = page.get_by_text("Remove from community", exact=True)
                await remove_item.wait_for(state="visible", timeout=5000)
                await remove_item.click()

                confirm_btn = page.get_by_role("button", name="Remove", exact=True)
                await confirm_btn.wait_for(state="visible", timeout=5000)
                await confirm_btn.click()

                removed.append(name)
                print(f"Removed {name}")
            except PlaywrightTimeoutError:
                print(f"Failed to remove {name} (UI element not found), skipping.")
            await asyncio.sleep(REMOVE_DELAY_SECONDS)

        with REMOVED_LOG_PATH.open("a") as f:
            f.write("\n".join(removed) + "\n")
        print(f"Done. Removed {len(removed)} member(s). Logged to {REMOVED_LOG_PATH.name}.")
