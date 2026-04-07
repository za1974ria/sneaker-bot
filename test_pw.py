from playwright.sync_api import sync_playwright

print("🚀 Test VPS correct")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)  # IMPORTANT

    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        viewport={"width": 1280, "height": 800},
        locale="fr-FR"
    )

    page = context.new_page()

    print("🔄 Chargement...")

    page.goto("https://www.zalando.fr", timeout=60000)

    page.wait_for_timeout(8000)

    html = page.content()

    print("📦 Taille HTML :", len(html))
    print("🧠 Contient Nike ?", "Nike" in html)

    browser.close()