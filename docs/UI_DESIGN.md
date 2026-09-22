# UI simplification contract

## Roles and visual language

- Finn is bookkeeping: uploads, processing, transactions, reconciliation, account statements and closing a period. Use blue.
- Nancy is planning and analysis: insights, budgets, goals and financial questions. Use orange.
- Income/expense signs and warnings retain their own semantic colors. Orange does not mean money out.
- Keep dark mode. Use flat surfaces, restrained borders and compact headings. No gradients, glow, glass blur or floating card shadows.
- Ordinary controls are small-radius rectangles, not pills. Keep visible keyboard focus and touch targets of at least 44 pixels.

## Navigation and capture

The primary bar contains Home, Activity, Upload, Insights and More. Chat and Goals remain available in More; they do not each need a permanent mobile tab.

Processing has its own `/processing` page. Do not mount the device queue over other pages, even as a collapsed panel. A selected upload produces one short, dismissible notification linking to Processing. Normal notifications expire after eight seconds, with focus/hover protection. Storage errors remain visible until dismissed.

Keep capture durability separate from presentation. Device originals remain pending until the server acknowledges its durable copy. Stable capture IDs, retries, recovery after offline reload, and server processing continue even when the queue page is not open. Never substitute a toast for the persisted queue. The processing page is available offline through the existing service worker.

The page includes device-owned pending files and uploaded server documents. Suppress duplicate display of an acknowledged local record when its server document is already visible; never suppress a device-owned original. Server history is paginated with unfinished documents first. Retry and review stay accessible.

## Audit findings and decisions

| Problem | Decision |
| --- | --- |
| Global outbox covers the phone screen, including when collapsed | Remove it from the shell; move status and recovery controls into Processing |
| Seven mobile destinations compete with an oversized glowing upload button | Keep five primary destinations; move Chat and Goals to More |
| Orange labels money out and bookkeeping review, weakening Finn/Nancy roles | Assign accents by function, not money direction or whether AI was involved |
| Gradients, glow, large radii and hero type compete with the financial data | Use a flat dark surface system and compact, responsive spacing |
| Eyebrow, title and tagline repeat the same idea | Keep a clear heading; keep supporting copy only when it changes a decision |
| Empty chart/category containers and duplicate summary values add scrolling | Omit empty decoration and reduce the home summary |
| Upload UI explains storage implementation before letting the user act | Use Upload; retain a short warning to keep originals until Saved |
| Device and server status can duplicate the same uploaded file | Show a visible server record once while preserving unacknowledged device recovery |

## Boundaries

This is presentation and navigation cleanup, not removal of accounting capabilities. Do not delete reconciliation, evidence, review, retry, approvals or close checks to make a screen look empty. Do not conceal incomplete totals or change their arithmetic. Suggestions remain suggestions.

Chat, goals and budgeting are secondary destinations, not proven useless features. Further feature removal needs usage evidence and an explicit product decision. Do not add a second dashboard, onboarding tour, explanatory card stack or queue framework as a replacement for the clutter removed here.

## Verification

Use generated fixtures only. Test pending upload, offline reload, lost acknowledgement, retry, storage failure, multi-file batches and accessible notification dismissal. Verify mobile geometry on the actual rendered pages, not only stylesheet strings. Check Finn/Nancy colors, focus, absence of page-wide overflow, and that no processing panel appears on Home or Activity. Run the canonical repository gates and `scripts/smoke-capture-browser` before handoff.
