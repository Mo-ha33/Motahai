import asyncio
import json
from playwright.async_api import async_playwright

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from build_tariq_payload import TARIQ_PROMPT  # single source of truth

json_prompt = TARIQ_PROMPT

json_str = json.dumps(json_prompt, indent=2, ensure_ascii=False)

async def run():
    async with async_playwright() as p:
        print("Connecting to Chrome on 9222...")
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        
        # Open a BRAND NEW page so we don't disturb the running scene!
        print("Creating brand new tab...")
        new_page = await context.new_page()
        
        url = 'https://prod.wesam.ai/w/ws_5b60c2ea0ba641dd/builder/tariq-ai-technical-m-8728'
        print(f"Navigating to {url}...")
        await new_page.goto(url, wait_until='networkidle')
        await new_page.wait_for_timeout(3000)
        
        print("Looking for textarea...")
        textarea = new_page.locator("textarea[placeholder*='Aya'], textarea").first
        count = await textarea.count()
        print("Textarea found count:", count)
        
        if count > 0:
            current_val = await textarea.input_value()
            print("Current prompt length:", len(current_val))
            
            print("Injecting JSON System Prompt...")
            await textarea.fill(json_str)
            await new_page.wait_for_timeout(3000)
            
            # Check for save or publish buttons
            print("Checking publish/save buttons...")
            pub_tab = new_page.locator('[data-testid="builder-view-publish"]').first
            if await pub_tab.count() > 0:
                print("Clicking publish tab...")
                await pub_tab.click()
                await new_page.wait_for_timeout(2000)
                
                confirm_btn = new_page.locator('button:has-text("Publish to marketplace")').last
                if await confirm_btn.count() > 0:
                    await confirm_btn.click()
                    print("Clicked 'Publish to marketplace'!")
                    await new_page.wait_for_timeout(4000)
                else:
                    print("Confirm button not found on publish tab.")
                
                # Navigate back to builder
                await new_page.goto(url, wait_until='networkidle')
                await new_page.wait_for_timeout(3000)
            
            # Take screenshot of the new tab
            screenshot_path = 'd:/GTM AI Crwue/tariq_json_prompt_applied.png'
            await new_page.screenshot(path=screenshot_path)
            print(f"Screenshot saved to {screenshot_path}")
            
        else:
            print("Textarea not found on page.")
            await new_page.screenshot(path='d:/GTM AI Crwue/tariq_builder_not_found.png')

if __name__ == '__main__':
    asyncio.run(run())
