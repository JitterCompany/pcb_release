# Getting datasheets without asking a human

Measured 2026-08-21 from this host. The short version: **it is not the
User-Agent**, so no amount of header tweaking fixes it.

## What actually happens

| host | result |
|---|---|
| ti.com | **works** — plain `urllib`, HTTP/1.1, any UA |
| quectel.com | reachable, but downloads are gated behind a portal |
| mouser.com | **bot-blocked** — returns an HTML page, "Access to this page has been denied" |
| st.com | **blocked after TLS** — see below |

For `www.st.com`, every layer up to and including TLS succeeds, and then the
request dies:

    DNS      -> 2.16.6.202  (e40218.a.akamaiedge.net)
    TCP:443  -> connect OK, 20 ms
    TLS      -> handshake OK, 74 ms
    HTTP/2   -> "stream 0 was not closed cleanly: INTERNAL_ERROR (err 2)"

Forcing HTTP/1.1 instead just hangs for the full timeout. `curl` and Python fail
identically despite having completely different TLS stacks and both sending a
full Chrome header set (`Sec-Fetch-*`, `Accept-Language`,
`Upgrade-Insecure-Requests`, the lot). The bare `st.com` domain answers, but only
to 301 back to the blocked `www.st.com`.

That signature — TLS completes, then the HTTP/2 connection is torn down — is
Akamai Bot Manager rejecting the client on its **TLS/HTTP-2 fingerprint** (JA3 /
SETTINGS frame ordering), which is a property of the TLS library, not of any
header we control. Note `ti.com` is *also* behind Akamai and works fine, so this
is ST's policy choice rather than a CDN-wide rule.

## Options, best first

1. **Distributor APIs.** Mouser / DigiKey / Nexar all expose datasheet URLs and
   are *designed* for programmatic access — an API key is the sanctioned way in,
   and it sidesteps the website's bot rules entirely. Mouser's website blocks us,
   but its API is a different, keyed endpoint. This is the route to try first,
   and it is already on the roadmap for price/lifecycle data anyway.

2. **Drive a real browser.** Claude Code's `claude-in-chrome` integration
   automates the user's actual Chrome, with its real session and real TLS stack.
   Nothing is spoofed — it *is* a browser — so bot detection has nothing to
   object to. Needs per-site permission granted in the extension. This turns
   "the bot asks the human to download a file" into "the bot uses the browser",
   which was the point.

3. **Human drop.** What we do today: `fetch` reports exactly which PDFs it could
   not get, batched, and picks them up from `<project>/datasheets/` next run.
   Fine as a fallback, irritating as a routine.

4. **TLS-impersonating clients** (`curl-impersonate`, `curl_cffi`) replicate
   Chrome's fingerprint from a script and would work. Recorded for completeness,
   but it is deliberately defeating an access control the vendor put there on
   purpose, and it breaks whenever they retune it. Prefer 1 and 2.

## Watch out

A PDF handed over by a browser may land as mode `0400` owned by the downloading
user. `bomcheck` then cannot read it and will report the part as having no
datasheet at all. If a dropped file seems ignored, check permissions first:

    chmod 664 <file>.pdf && chgrp dev <file>.pdf
