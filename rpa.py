import asyncio
import re
from pathlib import Path

import yaml
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

USER_DATA_DIR = Path(__file__).parent / ".camoufox-profile"
CONFIG_PATH = Path(__file__).parent / "config.yaml"
REMOVED_LOG_PATH = Path(__file__).parent / "removed_members.log"
FAILURE_SCREENSHOT_PATH = Path(__file__).parent / "failure_screenshot.png"
# A cold profile's first load (no cached WhatsApp assets yet) can take much
# longer than a warm one, so this is generous on purpose.
PAGE_LOAD_TIMEOUT_MS = 60000
# One-time per-run UI transitions (open community, open members dialog) - a
# community with a lot of messages/media to render can be slow, and each of
# these only happens once, so it's cheap to be generous.
NAV_TIMEOUT_MS = 20000

ADMIN_MARKER_SELECTOR = (
    '[data-testid="community-creator-marker"], [data-testid="community-admin-marker"]'
)
# Delay between removals so WhatsApp doesn't flag the account for bulk actions.
REMOVE_DELAY_SECONDS = 3


class AsyncCamoufoxClient:
    async def run(self) -> None:
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        search_term = config["search"]

        async with AsyncCamoufox(
            headless=False,
            persistent_context=True,
            user_data_dir=str(USER_DATA_DIR),
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto("https://web.whatsapp.com/")

                await self._wait_for_login(page)
                members_dialog, total_members = await self._open_members_dialog(page, search_term)
                await self._remove_non_admins(page, members_dialog, total_members)

                print("Browser stays open. Close it or press Ctrl+C to stop.")
                await context.wait_for_event("close", timeout=0)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    async def _wait_for_login(self, page: Page) -> None:
        qr_code_selector = '[data-testid="link-device-qr-code"]'
        search_box_selector = 'input[aria-label="Search or start a new chat"]'
        qr_code = page.locator(qr_code_selector)

        # Race both: waiting for the QR *alone* and then falling back to
        # "already logged in" on timeout means a logged-in session always
        # burns the full timeout first, since a negative wait can only
        # resolve once the deadline is reached, never early.
        try:
            await page.locator(f"{qr_code_selector}, {search_box_selector}").first.wait_for(
                state="visible", timeout=PAGE_LOAD_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            await page.screenshot(path=str(FAILURE_SCREENSHOT_PATH))
            print(
                f"Neither the QR code nor the chat list showed up (page title: "
                f"{await page.title()!r}). Saved {FAILURE_SCREENSHOT_PATH.name}."
            )
            raise

        if await qr_code.count() == 0:
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

    async def _open_members_dialog(
        self, page: Page, search_term: str
    ) -> tuple[Locator, int | None]:
        search_box = page.get_by_role("textbox", name="Search or start a new chat")
        try:
            await search_box.wait_for(state="visible", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await page.screenshot(path=str(FAILURE_SCREENSHOT_PATH))
            print(
                f"The chat list never showed up (page title: {await page.title()!r}). "
                f"Saved {FAILURE_SCREENSHOT_PATH.name} - check what the browser actually "
                "displayed."
            )
            raise
        await search_box.fill(search_term)
        await page.wait_for_timeout(2000)

        result = page.get_by_title(search_term, exact=True).first
        await result.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        await result.click()
        await page.wait_for_timeout(1500)

        # The "Manage community" text link only exists in the one-time
        # "Welcome to your community!" banner - gone on later visits.
        # The header is always there and opens the same info panel.
        header = page.locator('[data-testid="conversation-info-header"]')
        await header.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        await header.click()
        await page.wait_for_timeout(1500)

        # The info panel opens on whichever tab matches the sub-chat we
        # came from (e.g. "Announcements") - the member list only lives
        # under "Community", so switch to it explicitly.
        community_tab = page.get_by_role("tab", name="Community", exact=True)
        if await community_tab.count() > 0:
            await community_tab.click()
            await page.wait_for_timeout(500)

        members_header = page.get_by_text("community members", exact=False).first
        await members_header.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        header_text = await members_header.text_content() or ""
        match = re.match(r"\s*(\d+)", header_text)
        total_members = int(match.group(1)) if match else None

        members_dialog = await self._open_members_list(page)
        return members_dialog, total_members

    async def _open_members_list(self, page: Page) -> Locator:
        # Clicking the "N community members" header opens a dedicated,
        # unvirtualized-by-clutter "Members (N)" dialog - easier to scroll
        # and scope queries into than the mixed community-info sidebar.
        members_header = page.get_by_text("community members", exact=False).first
        await members_header.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        await members_header.click()

        members_dialog = page.get_by_role("dialog", name="Members", exact=False)
        await members_dialog.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        return members_dialog

    async def _find_next_removable_row(
        self, scope: Locator, skip_names: set[str]
    ) -> tuple[Locator, str] | None:
        """Returns the first currently-rendered row that isn't an admin/owner
        and isn't in skip_names (rows we already failed to remove)."""
        for row in await scope.locator('[data-testid="cell-frame-container"]').all():
            title_el = row.locator('[data-testid="cell-frame-title"] span[title]').first
            try:
                name = await title_el.get_attribute("title", timeout=1000)
            except PlaywrightTimeoutError:
                continue
            if not name or name in skip_names:
                continue
            if await row.locator(ADMIN_MARKER_SELECTOR).count() > 0:
                continue
            return row, name
        return None

    async def _remove_non_admins(
        self, page: Page, scope: Locator, total_members: int | None
    ) -> None:
        note = f" ({total_members} total members)" if total_members is not None else ""
        print(f"Removing every non-admin member{note}, keeping the owner and admins.")

        # Single pass: remove whatever non-admin is currently visible, only
        # scroll for more once nothing removable is on screen. Removing a
        # row reflows the (virtualized) list, so the next target is often
        # already visible without scrolling at all - and since nothing is
        # looked up by name after the fact, there's no risk of a stale
        # match landing on the wrong row (or opening a contact instead).
        await scope.hover()
        removed_count = 0
        failed_names: set[str] = set()
        stagnant_rounds = 0
        max_rounds = max(1000, (total_members or 50) * 4)

        for _ in range(max_rounds):
            if stagnant_rounds >= 5:
                break

            found = await self._find_next_removable_row(scope, failed_names)
            if found is None:
                stagnant_rounds += 1
                await page.mouse.wheel(0, 400)
                await page.wait_for_timeout(500)
                continue

            row, name = found
            try:
                # Clicking the row opens its full dropdown (Message/View/
                # Verify/Make admin/Remove) directly - there is no separate
                # context-btn to click first (confirmed against the live
                # DOM: 0 context-btn matches after this click).
                await row.click()

                # The context menu and confirm dialog render as page-level
                # overlays, outside the members dialog's own DOM subtree.
                # Matched by data-testid, not text: WhatsApp leaves a stale
                # duplicate "Remove from community" text node around during
                # the menu's open/close animation, and matching by text hit
                # both, making the click land on the wrong (dead) one.
                remove_item = page.locator('[data-testid="remove-from-community-identity"]').first
                await remove_item.wait_for(state="visible", timeout=8000)
                await remove_item.click()

                confirm_btn = page.get_by_role("button", name="Remove", exact=True).first
                await confirm_btn.wait_for(state="visible", timeout=8000)
                await confirm_btn.click()

                removed_count += 1
                with REMOVED_LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(name + "\n")
                print(f"Removed {name} ({removed_count} so far)")
                stagnant_rounds = 0
                await asyncio.sleep(REMOVE_DELAY_SECONDS)

                # Removing a member closes the Members dialog back to the
                # plain Community info panel (confirmed live: the dialog's
                # role="dialog" node is simply gone right after) - reopen
                # it before looking for the next target.
                scope = await self._open_members_list(page)
                await scope.hover()
            except PlaywrightTimeoutError:
                print(f"Failed to remove {name}, skipping.")
                failed_names.add(name)
        else:
            print(f"Stopped after {max_rounds} rounds.")

        print(f"Done. Removed {removed_count} member(s). Logged to {REMOVED_LOG_PATH.name}.")
