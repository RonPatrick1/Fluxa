const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
    testDir: "./tests",
    timeout: 60_000,
    workers: 1,
    use: { headless: true },
    projects: [
        {
            name: "chrome",
            use: { browserName: "chromium", channel: "chrome" },
        },
        {
            name: "firefox",
            use: { browserName: "firefox" },
        },
    ],
});
