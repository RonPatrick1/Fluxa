const { test, expect } = require("@playwright/test");

test("unsupported local video plays through a bounded compatibility stream", async ({ page }) => {
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));

    await page.goto("http://127.0.0.1:8097/");
    await page.locator('[data-playlist="10"]').click();
    await page.locator('[data-media-id="605"]').click();

    const video = page.locator("#player-stage video");
    await expect(video).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#player-technical")).toContainText("H.264 + AAC");
    await expect(page.locator(".player-time")).toContainText("/ 28:40");
    await expect.poll(() => video.evaluate(element => element.readyState), {
        timeout: 30_000,
    }).toBeGreaterThanOrEqual(3);
    await expect.poll(
        () => video.evaluate(element => element.currentTime),
        { timeout: 15_000 }
    ).toBeGreaterThan(0.25);
    await expect(page.locator(".player-control-button")).toHaveCount(12);
    expect(await page.locator(".player-control-button").allTextContents()).toEqual(
        Array(12).fill("")
    );
    await expect(page.locator(".stream-loading")).toHaveCount(0);
    expect(await video.evaluate(element => ({
        muted: element.muted,
        volume: element.volume,
    }))).toEqual({ muted: false, volume: 1 });
    await page.getByRole("button", { name: "Mute" }).click();
    await expect.poll(() => video.evaluate(element => element.muted)).toBe(true);
    await page.getByRole("button", { name: "Unmute" }).click();
    await expect.poll(() => video.evaluate(element => element.muted)).toBe(false);
    await page.getByRole("button", { name: "Enter full screen" }).click();
    await expect.poll(() => page.evaluate(() => (
        document.fullscreenElement === document.documentElement
    ))).toBe(true);
    await expect(page.getByRole("button", { name: "Exit full screen" })).toBeVisible();
    const panel = page.locator(".player-panel");
    await expect.poll(
        () => panel.evaluate(element => element.classList.contains("controls-hidden")),
        { timeout: 5_000 }
    ).toBe(true);
    await expect.poll(() => page.locator(".fluxa-player-controls").evaluate(element => ({
        opacity: getComputedStyle(element).opacity,
        pointerEvents: getComputedStyle(element).pointerEvents,
    }))).toEqual({ opacity: "0", pointerEvents: "none" });
    await expect.poll(() => page.locator("#close-player").evaluate(element => (
        getComputedStyle(element).opacity
    ))).toBe("0");
    await page.mouse.move(160, 160);
    await expect(panel).not.toHaveClass(/controls-hidden/);
    await expect.poll(() => page.locator(".fluxa-player-controls").evaluate(element => (
        getComputedStyle(element).opacity
    ))).toBe("1");
    await page.getByRole("button", { name: "Exit full screen" }).click();
    await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false);
    await expect(page.getByRole("button", { name: "Enter full screen" })).toBeVisible();

    await page.getByRole("button", { name: "Enter full screen" }).click();
    await expect.poll(() => page.evaluate(() => (
        document.fullscreenElement === document.documentElement
    ))).toBe(true);
    await page.locator("#close-player").click();
    await expect.poll(() => page.evaluate(() => (
        document.fullscreenElement === document.documentElement
    ))).toBe(true);
    await expect(page.locator("#player-modal")).toBeHidden();
    await page.locator("#search-input").fill("still interactive");
    await expect(page.locator("#search-input")).toHaveValue("still interactive");
    await page.evaluate(() => document.exitFullscreen());
    await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false);
    await expect.poll(async () => {
        const response = await page.request.get("http://127.0.0.1:8097/api/status");
        return (await response.json()).compatibility.active_sessions;
    }).toBe(0);
    expect(pageErrors).toEqual([]);
});

test("lookahead audio stays interleaved across HLS segments", async ({ page }) => {
    const hlsErrors = [];
    page.on("console", message => {
        if (message.text().startsWith("Fluxa HLS")) {
            hlsErrors.push(message.text());
        }
    });
    await page.route("**/api/media/334/compatibility", async route => {
        if (route.request().method() !== "POST") {
            await route.continue();
            return;
        }
        const body = route.request().postDataJSON();
        body.start_ms = 2_628_126;
        await route.continue({ postData: JSON.stringify(body) });
    });

    await page.goto("http://127.0.0.1:8097/");
    await page.locator("#search-input").fill("Rory's Dance");
    await page.locator('[data-media-id="334"]').click();
    const video = page.locator("#player-stage video");
    await expect.poll(
        () => video.evaluate(element => element.currentTime),
        { timeout: 30_000 }
    ).toBeGreaterThan(12);
    expect(await video.evaluate(element => ({
        error: element.error && element.error.message,
        paused: element.paused,
        readyState: element.readyState,
    }))).toEqual({ error: null, paused: false, readyState: 4 });
    expect(hlsErrors).toEqual([]);

    await page.locator("#close-player").click();
});

test("chapters and image captions restart at the full-program position", async ({ page }) => {
    await page.goto("http://127.0.0.1:8097/");
    await page.locator('[data-playlist="8"]').click();
    await page.locator('[data-media-id="9567"]').click();

    await expect(page.locator(".player-time")).toContainText("/ 22:26", {
        timeout: 20_000,
    });
    await expect(page.getByRole("button", { name: "Previous video" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Next video" })).toBeEnabled();
    await page.getByRole("button", { name: "Next video" }).click();
    await expect(page.locator("#player-technical")).toContainText("Playlist item 4 of 186", {
        timeout: 20_000,
    });
    await page.getByRole("button", { name: "Previous video" }).click();
    await expect(page.locator("#player-technical")).toContainText("Playlist item 3 of 186", {
        timeout: 20_000,
    });
    await page.getByRole("button", { name: "Show chapters" }).click();
    await expect(page.locator(".chapter-card")).toHaveCount(5);
    await expect.poll(
        () => page.locator(".chapter-card img").nth(1).getAttribute("src"),
        { timeout: 30_000 }
    ).toBeTruthy();
    let delayedRestart = false;
    await page.route("**/api/media/9567/compatibility", async route => {
        if (!delayedRestart && route.request().method() === "POST") {
            delayedRestart = true;
            await new Promise(resolve => setTimeout(resolve, 900));
        }
        await route.continue();
    });
    await page.locator('.chapter-card[data-start-ms="314731"]').click();
    await expect.poll(() => page.locator(".stream-hold").evaluate(image => (
        image.complete && image.naturalWidth > 0
    )), { timeout: 5_000 }).toBeTruthy();
    await expect(page.locator(".stream-loading")).toHaveCount(0);
    await expect.poll(async () => {
        const seconds = Number(await page.locator(".player-seek").inputValue());
        return seconds >= 314 && seconds < 325;
    }, { timeout: 20_000 }).toBeTruthy();
    await page.unroute("**/api/media/9567/compatibility");
    await expect(page.locator(".player-time")).toContainText("/ 22:26");
    await expect.poll(
        () => page.locator("#player-stage video").evaluate(element => element.currentTime),
        { timeout: 15_000 }
    ).toBeGreaterThan(0.25);

    await page.getByRole("button", { name: "Next chapter" }).click();
    await expect.poll(async () => {
        const seconds = Number(await page.locator(".player-seek").inputValue());
        return seconds >= 832 && seconds < 843;
    }, { timeout: 20_000 }).toBeTruthy();
    await expect(page.locator(".player-time")).toContainText("/ 22:26");
    await expect.poll(
        () => page.locator("#player-stage video").evaluate(element => element.currentTime),
        { timeout: 15_000 }
    ).toBeGreaterThan(0.25);

    await page.getByRole("button", { name: "Playback settings" }).click();
    await page.locator("#captions-current").check();
    await expect(page.locator("#player-technical")).toContainText("captions burned in", {
        timeout: 20_000,
    });
    await expect(page.locator("#caption-track")).toHaveValue("0");
    await expect.poll(
        () => page.locator("#player-stage video").evaluate(element => element.currentTime),
        { timeout: 15_000 }
    ).toBeGreaterThan(0.25);
    await page.locator("#captions-global").check();
    expect(await page.evaluate(() => localStorage.getItem("fluxa-captions-global"))).toBe("on");

    await page.locator("#close-player").click();
    await page.locator('[data-media-id="9566"]').click();
    await expect(page.locator("#captions-current")).toBeChecked();
    await expect(page.locator("#player-technical")).toContainText("captions burned in", {
        timeout: 20_000,
    });
    await page.locator("#close-player").click();
    await expect.poll(async () => {
        const response = await page.request.get("http://127.0.0.1:8097/api/status");
        return (await response.json()).compatibility.active_sessions;
    }).toBe(0);
});

test("text captions can be selected without burning them into video", async ({ page }) => {
    await page.goto("http://127.0.0.1:8097/");
    await page.locator("#search-input").fill("K19");
    await page.locator('[data-media-id="10"]').click();
    await page.getByRole("button", { name: "Playback settings" }).click();
    await expect(page.locator("#caption-track option")).toHaveCount(3, { timeout: 20_000 });
    await page.locator("#caption-track").selectOption("2");
    await page.locator("#captions-current").check();

    const video = page.locator("#player-stage video");
    await expect.poll(() => video.evaluate(element => {
        const tracks = Array.from(element.textTracks);
        return tracks.length && tracks[0].mode === "showing"
            && tracks[0].cues && tracks[0].cues.length > 0;
    }), { timeout: 20_000 }).toBeTruthy();
    await expect(page.locator("#player-technical")).not.toContainText("captions burned in");

    await page.locator("#close-player").click();
    await expect.poll(async () => {
        const response = await page.request.get("http://127.0.0.1:8097/api/status");
        return (await response.json()).compatibility.active_sessions;
    }).toBe(0);
});
