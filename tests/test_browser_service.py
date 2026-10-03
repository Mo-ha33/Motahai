"""
Unit tests for Browser Service & QA Sniffer
"""

import pytest
from src.ameen_workforce.browser_service import BrowserSnifferService

@pytest.mark.asyncio
async def test_pii_detection_and_scorecard():
    sniffer = BrowserSnifferService()
    
    # Test markup with Egyptian National ID violation
    violating_html = """
    <html>
        <body>
            <h1>Customer Record</h1>
            <p>National ID: 29801011234567</p>
        </body>
    </html>
    """
    
    # Monkeypatch httpx get
    class MockResponse:
        status_code = 200
        text = violating_html
    
    async def mock_get(*args, **kwargs):
        return MockResponse()
    
    import httpx
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    
    result = await sniffer.inspect_target_url("https://client.motahai.com/test")
    assert result["pdpl_151_2020_compliance"] == "FAIL"
    assert len(result["pii_violations"]) > 0
    assert result["scorecard"]["privacy_score"] < 70
    monkeypatch.undo()

@pytest.mark.asyncio
async def test_clean_markup_passes_pdpl():
    sniffer = BrowserSnifferService()
    
    clean_html = """
    <html>
        <head>
            <script>gtag('consent', 'default', {'ad_storage': 'granted'});</script>
            <script src="https://www.googletagmanager.com/gtm.js?id=GTM-TEST999"></script>
        </head>
        <body>
            <h1>Store Catalog</h1>
        </body>
    </html>
    """
    
    class MockResponse:
        status_code = 200
        text = clean_html
        
    async def mock_get(*args, **kwargs):
        return MockResponse()
        
    import httpx
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    
    result = await sniffer.inspect_target_url("https://client.motahai.com/catalog")
    assert result["pdpl_151_2020_compliance"] == "PASS"
    assert result["consent_mode_v2_active"] is True
    assert "GTM-TEST999" in result["detected_gtm_containers"]
    assert result["scorecard"]["privacy_score"] >= 95
    monkeypatch.undo()
