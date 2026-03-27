import os                  # os: standard library module for environment variable access
import asyncio             # asyncio: standard library for asynchronous I/O operations
import json                # json: stadard library for serializing/deserializing JSON data
from typing import List, Optional  # typing: provides type hints for static analysis
from dotenv import load_dotenv  # dotenv: third-party package to load .env file into os.environ
load_dotenv()              # load_dotenv(): reads .env and populates environment variables

from bs4 import BeautifulSoup  # BeautifulSoup: HTML/XML parser from bs4 package
from urllib.parse import urljoin, urldefrag, urlparse  # urllib.parse: URL manipulation utilities

# LangChain
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage  
# langchain_core.messages: immutable message classes for LLM conversation history
from langchain_core.tools import BaseTool  
# langchain_core.tools: base class for all tool implementations

# OpenAI
from langchain_openai import ChatOpenAI  # langchain_openai: wrapper for OpenAI chat completions API

# Playwright
from playwright.async_api import async_playwright  # playwright.async_api: async browser automation API

# ------------------------------
# Utilities (unchanged)
# ------------------------------
def normalize_url(base: str, link: str) -> Optional[str]:
    # base: str - parameter holding the base URL (absolute) used for urljoin()
    # link: str - parameter holding the relative or absolute URL string to normalize
    # return: Optional[str] - normalized absolute URL or None if invalid
    if not link:
        return None
    link = urljoin(base, link.strip())  # urljoin(): joins base with link, handling relative paths
    link, _ = urldefrag(link)          # urldefrag(): removes fragment identifier (#...) from URL
    parsed = urlparse(link)            # urlparse(): breaks URL into scheme, netloc, path, etc.
    if parsed.scheme not in {"http", "https"}:
        return None
    return link

def extract_main_text(html: str) -> str:
    # html: str - parameter containing raw HTML source code of the page
    # return: str - cleaned main textual content (headings, paragraphs, lists)
    soup = BeautifulSoup(html, "lxml")  # BeautifulSoup(): creates parse tree from HTML string
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()  # decompose(): removes tag and all its children from the soup tree
    container = soup.select_one("article, main, .content, .post, .entry, #content") or soup
    blocks = []
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "code"]):
        text = el.get_text(" ", strip=True)  # get_text(): extracts text from element, joins with space
        if text and len(text.split()) > 2:   # filter: discard very short or empty text blocks
            blocks.append(text)
    return "\n".join(blocks).strip()  # join(): combines blocks with newlines; strip(): removes leading/trailing whitespace

# ------------------------------
# Playwright Browser Tool (unchanged)
# ------------------------------
class PlaywrightBrowseTool(BaseTool):
    # BaseTool: parent class providing invoke(), name, description, and schema generation
    name: str = "browse_page"  # name: str - tool name exposed to the LLM via tool calling
    description: str = (
        "Open a webpage and extract its text content and on-page links. "
        "Parameters: url (required), clicks (optional list of CSS selectors), "
        "wait_selector (optional CSS selector). "
        "Returns JSON: {title, url, text, links}."
    )  # description: str - natural-language tool spec shown to the LLM

    def _run(self, url: str, clicks: Optional[List[str]] = None,
             wait_selector: Optional[str] = None, **kwargs) -> str:
        # _run: sync entry point required by LangChain BaseTool
        # url: str - required parameter: target webpage URL
        # clicks: Optional[List[str]] - optional list of CSS selectors to click
        # wait_selector: Optional[str] - optional CSS selector to wait for
        # **kwargs: catches any extra args passed by LLM
        # return: str - JSON string containing page data
        try:
            loop = asyncio.get_event_loop()  # get_event_loop(): retrieves current asyncio event loop
            if loop.is_running():
                import concurrent.futures  # concurrent.futures: thread pool for running async code in sync context
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, self._arun(url, clicks, wait_selector))
                    return future.result()
            else:
                return loop.run_until_complete(self._arun(url, clicks, wait_selector))
        except RuntimeError:
            return asyncio.run(self._arun(url, clicks, wait_selector))

    async def _arun(self, url: str, clicks=None, wait_selector=None) -> str:
        # _arun: async coroutine that performs actual browser automation
        # url: str - target URL passed from _run()
        # clicks: list or None - CSS selectors to click sequentially
        # wait_selector: str or None - CSS selector to wait for after clicks
        # return: str - JSON-encoded dict with title, url, text, links
        clicks = clicks or []  # default: empty list if None
        async with async_playwright() as p:  # async_playwright(): context manager for Playwright instance
            browser = await p.chromium.launch(headless=True)  # launch(): starts Chromium browser in headless mode
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )  # new_context(): creates isolated browser context with custom user-agent
            await context.route("**/*", lambda route: route.abort()
                                if route.request.resource_type in {"image", "font", "media"} else route.continue_())
            # route(): aborts non-essential resource types to speed up scraping
            page = await context.new_page()  # new_page(): creates new tab/page in the context
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                # goto(): navigates to URL and waits until DOMContentLoaded event
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass  # networkidle may timeout on heavy sites - ignore
                for sel in clicks:
                    try:
                        await page.click(sel, timeout=10000)  # click(): performs mouse click on CSS selector
                        await page.wait_for_timeout(800)       # wait_for_timeout(): artificial delay after click
                    except Exception:
                        pass
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=10000)
                    except Exception:
                        pass
                html = await page.content()  # content(): returns full HTML source as string
                cur_url = page.url            # page.url: current resolved URL after redirects
                title = await page.title()    # title(): returns <title> tag text
            finally:
                await browser.close()  # close(): terminates browser process (cleanup)

            text = extract_main_text(html)  # extract_main_text(): utility to strip boilerplate
            soup = BeautifulSoup(html, "lxml")  # re-parse HTML for link extraction
            origin = urlparse(cur_url).netloc  # netloc: domain part used to filter same-site links
            links = []
            seen = set()  # seen: set for O(1) duplicate elimination
            for a in soup.find_all("a", href=True):  # find_all(): collects all <a href=...> tags
                u = normalize_url(cur_url, a["href"])  # normalize_url(): makes absolute and same-origin
                if u and urlparse(u).netloc == origin and u not in seen:
                    seen.add(u)
                    links.append(u)
            return json.dumps({"title": title, "url": cur_url, "text": text, "links": links})
            # json.dumps(): serializes dict to JSON string (required by LangChain tool calling)

# ------------------------------
# LLM
# ------------------------------
def get_llm():
    # get_llm(): factory function that returns configured ChatOpenAI instance
    # return: ChatOpenAI - LLM object bound to OpenAI API with model and temperature
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),  # os.getenv(): reads model name from env (fallback)
        temperature=0.2,  # temperature: float controlling randomness (0.2 = mostly deterministic)
    )

# ------------------------------
# Simple ReAct Loop + Report
# ------------------------------
def research_and_report(target_url: str) -> str:
    # target_url: str - parameter: starting cyberdaily.au article URL
    # return: str - final Markdown security report
    tools = [PlaywrightBrowseTool()]  # tools: list of BaseTool instances passed to LLM
    llm = get_llm().bind_tools(tools)  # bind_tools(): attaches tool schemas to LLM for function calling

    # Agent system prompt (same rules as original)
    agent_system = SystemMessage(content=(
        "You are an autonomous security intelligence research agent. "
        "Your ONLY job is to browse the given URL and extract real, factual information for these four sections:\n"
        " 1) Vulnerability Details\n"
        " 2) Exploitation Status\n"
        " 3) Impact / Risk\n"
        " 4) Indicators of Compromise (IoCs)\n\n"
        "Rules:\n"
        "- Use browse_page to fetch the target URL immediately.\n"
        "- Stay strictly on cyberdaily.au. Do NOT follow links to external domains.\n"
        "- Only follow additional same-domain links if they clearly provide more data for the four sections.\n"
        "- Max 5 pages total (including the starting page).\n"
        "- Extract only real data from the page. Do not invent or hallucinate anything.\n"
        "- Once you have enough real data (or hit the page limit), stop calling tools and summarize "
        "ALL the raw extracted facts clearly so the report writer can compile the final report.\n"
        "- Your final message MUST contain all extracted facts, CVE IDs, dates, versions, "
        "exploit status, affected systems, IoCs, and source URLs."
    ))  # SystemMessage: immutable system instruction for the LLM

    # Starting query (same as original)
    query = (
        f"Browse this URL and extract all available information for these four sections: "
        f"Vulnerability Details, Exploitation Status, Impact / Risk, Indicators of Compromise (IoCs).\n"
        f"URL: {target_url}\n\n"
        f"Only click additional links on cyberdaily.au if they clearly contain more data for those sections. "
        f"Do not use any placeholder or invented data."
    )  # query: str - user message that triggers the agent loop

    messages: List = [agent_system, HumanMessage(content=query)]  # messages: List[BaseMessage] - conversation history

    page_count = 0  # page_count: int - counter enforcing max 5 page visits
    while page_count < 5:
        response = llm.invoke(messages)  # invoke(): sends messages to LLM, returns AIMessage (may contain tool_calls)
        messages.append(response)       # append: adds LLM response to history for next turn

        if not response.tool_calls:     # tool_calls: list of tool call dicts (empty means agent is done)
            break  # Agent finished → has summary

        # Execute every tool call
        for tool_call in response.tool_calls:  # tool_call: dict with name, args, id from LLM
            tool = next(t for t in tools if t.name == tool_call["name"])  # next(): finds matching tool instance
            tool_result = tool.invoke(tool_call["args"])  # invoke(): calls _run() (or _arun internally)
            messages.append(ToolMessage(
                content=tool_result,
                tool_call_id=tool_call["id"],  # tool_call_id: required to correlate response with call
                name=tool_call["name"]
            ))
        page_count += 1  # increment after each tool execution (each browse_page = 1 page)

    # Build evidence from all ToolMessages + final agent summary
    evidence_parts = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.content:  # isinstance(): runtime type check
            try:
                parsed = json.loads(m.content)  # json.loads(): parses tool result back to dict
                evidence_parts.append(
                    f"[PAGE: {parsed.get('url', '')}]\nTitle: {parsed.get('title', '')}\n{parsed.get('text', '')}"
                )
            except Exception:
                evidence_parts.append(m.content)
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            evidence_parts.append(f"[AGENT SUMMARY]:\n{m.content}")
    evidence = "\n\n---\n\n".join(evidence_parts) if evidence_parts else "No evidence gathered."

    # Reporter (same as original)
    reporter_llm = get_llm()  # reporter_llm: separate LLM instance for final report writing (no tools)
    reporter_system = (
        "You are a professional security report writer. "
        "Using ONLY the evidence provided below (do NOT invent or hallucinate any data), "
        "produce a structured Markdown report with exactly these sections:\n\n"
        "## Executive Summary\n"
        "## Vulnerability Details\n"
        "## Exploitation Status\n"
        "## Impact / Risk\n"
        "## Indicators of Compromise (IoCs)\n"
        "## Sources & Evidence\n"
        "## Next Steps\n\n"
        "If a section has no data in the evidence, write: *Not found in source.*\n"
        "Be concise and factual. Include CVE IDs, dates, versions, and IoCs exactly as found."
    )  # reporter_system: str - instructions for structured Markdown output

    report_messages = [
        SystemMessage(content=reporter_system),
        HumanMessage(content=f"EVIDENCE FROM SOURCE:\n\n{evidence}\n\nWrite the report now.")
    ]  # report_messages: fresh conversation for the reporter LLM
    report = reporter_llm.invoke(report_messages)  # invoke(): gets final Markdown string
    return report.content  # report.content: str - the generated Markdown report

# ------------------------------
# Run
# ------------------------------
if __name__ == "__main__":  # __name__ == "__main__": standard Python entry point guard
    target_url = "https://www.cyberdaily.au/security/13306-patch-now-exploitation-of-nginx-ui-vulnerability-imminent-warns-threat-analyst"
    # target_url: hardcoded example URL for demonstration

    print("Starting research (max 5 pages on cyberdaily.au)...")
    report = research_and_report(target_url)  # research_and_report(): main orchestration function

    print("\n===== FINAL REPORT =====\n")
    print(report)

    report_path = "report.md"  # report_path: str - filesystem path for output file
    with open(report_path, "w", encoding="utf-8") as f:  # open(): context-managed file handle in write mode
        f.write(report)
    print(f"\nReport saved to {report_path}")