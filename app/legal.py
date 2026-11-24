"""Public pages for platform app reviews: what Reelflow is, Terms of Service and Privacy Policy.

They describe a private, self-hosted tool used by one operator to publish their own content. The
operator's name and contact email come from Settings → Public pages. These are plain templates, not
legal advice.
"""
from datetime import date
from html import escape
from . import config as c
from .db import settings

STYLE = """<style>
:root{--paper:#f4f1e9;--ink:#202720;--muted:#62675e;--accent:#a43421;--line:#d6d8cc;color-scheme:light}
@media(prefers-color-scheme:dark){:root{--paper:#1a1d19;--ink:#ecebe4;--muted:#a3a79b;--accent:#e06a52;--line:#383c35}}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:720px;margin:0 auto;padding:48px 16px 80px}h1{font-size:34px;line-height:1.15;margin:0 0 8px}
h2{font-size:20px;margin:32px 0 8px}p,li{color:var(--ink)}.muted{color:var(--muted)}a{color:var(--accent)}
nav{display:flex;gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:28px}
</style>"""


def details():
    s = settings()
    name = s['legal_name'].strip() or s['brand_name']
    return {'name': escape(name), 'email': escape(s['legal_email'].strip()), 'site': escape(c.PUBLIC_URL),
            'updated': escape(s['legal_updated'] or date.today().isoformat())}


def page(title, body):
    d = details()
    contact = f'<a href="mailto:{d["email"]}">{d["email"]}</a>' if d['email'] else 'the operator'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)} · {d["name"]}</title>{STYLE}</head><body><main>'
            f'<nav><a href="/about">About</a><a href="/legal/terms">Terms of Service</a><a href="/legal/privacy">Privacy Policy</a></nav>'
            + body.format(**d, contact=contact) + '</main></body></html>')


ABOUT = """<h1>{name} publishing desk</h1>
<p>This is Reelflow, a private, self-hosted tool that {name} uses to prepare and schedule its own short videos
for its own Instagram, YouTube and TikTok accounts.</p>
<h2>What it does</h2>
<ul><li>Prepares video clips the operator filmed and writes draft captions for the operator to review.</li>
<li>Shows a schedule of upcoming posts, which the operator reviews, edits and approves.</li>
<li>Publishes approved posts to the operator's own connected accounts through each platform's official API.</li></ul>
<h2>Who can use it</h2>
<p>Only the operator, behind a password. It is not offered to the public, does not create accounts for other people,
and does not post to anyone else's account.</p>
<h2>TikTok</h2>
<p>With TikTok it uses Login Kit to connect the operator's own TikTok account and the Content Posting API to publish
videos the operator has reviewed and approved. Before each post the operator sees the account it will post to,
chooses who can see it, chooses whether comments, duets and stitches are allowed, can disclose branded or promotional
content, and agrees to TikTok's Music Usage Confirmation.</p>
<p class="muted">Contact: {contact} · Website: {site}</p>"""

TERMS = """<h1>Terms of Service</h1><p class="muted">Last updated {updated}</p>
<p>These terms cover the Reelflow publishing desk operated by {name} at {site} ("the service").</p>
<h2>1. Who may use the service</h2>
<p>The service is a private tool for {name}'s own content. Access is restricted to the operator by password. No one else
may sign in or use it, and it is not offered to the public.</p>
<h2>2. Connected accounts</h2>
<p>The operator connects only accounts they own or are authorised to manage, using each platform's official sign-in.
The service acts only on the operator's instructions and on posts the operator has reviewed and approved. The operator
can disconnect any account at any time in Settings, which stops all further posting to it.</p>
<h2>3. Content</h2>
<p>The operator is responsible for the videos, captions and other content they publish, and for having the rights to
them, including any music. Content is published under the terms of the platform it is posted to, including TikTok's
Terms of Service, Community Guidelines, Music Usage Confirmation and Branded Content Policy.</p>
<h2>4. Platform rules</h2>
<p>The service uses official APIs and follows each platform's developer terms. It does not collect data about other
users, scrape platforms, or automate actions on accounts the operator does not own.</p>
<h2>5. No warranty</h2>
<p>The service is provided as is. Platforms may change or refuse requests, and posts may be delayed or fail.</p>
<h2>6. Changes and contact</h2>
<p>These terms may be updated; the date above shows the latest version. Questions: {contact}.</p>"""

PRIVACY = """<h1>Privacy Policy</h1><p class="muted">Last updated {updated}</p>
<p>This policy explains what the Reelflow publishing desk operated by {name} at {site} ("the service") stores and why.
The service is a private tool used only by its operator to publish their own content.</p>
<h2>What is collected</h2>
<ul><li><strong>From connected platforms:</strong> for each account the operator connects, its account ID, username or
display name, and the access tokens needed to publish. From TikTok this means the open ID, display name, avatar
and the posting permissions TikTok grants (the user.info.basic and video.publish scopes).</li>
<li><strong>Content:</strong> the operator's own video clips, captions and schedule.</li>
<li><strong>Post results:</strong> post IDs, links and, where the platform provides them, view and engagement counts
for the operator's own posts.</li></ul>
<p>The service does not collect information about other platform users, viewers or followers, and does not use
cookies for tracking or advertising. It sets one sign-in cookie for the operator.</p>
<h2>How it is used</h2>
<p>Only to publish and schedule the operator's own posts and to show the operator how those posts perform.</p>
<h2>Where it is stored</h2>
<p>On the operator's own server. Access tokens are encrypted at rest. Videos are sent only to the platform they are
being posted to. When captions are written with AI, video frames and caption text are sent to the AI provider the
operator chooses; no platform account data is sent to it.</p>
<h2>Sharing</h2>
<p>Data is not sold or shared with anyone else.</p>
<h2>Retention and deletion</h2>
<p>Disconnecting an account in Settings deletes its tokens immediately. The operator can delete clips and posting
history at any time. The connected account owner can also revoke access in the platform's own settings (on TikTok:
Settings and privacy → Security → Manage app permissions).</p>
<h2>Contact</h2>
<p>Questions or deletion requests: {contact}.</p>"""


def about():
    return page('About', ABOUT)


def terms():
    return page('Terms of Service', TERMS)


def privacy():
    return page('Privacy Policy', PRIVACY)
