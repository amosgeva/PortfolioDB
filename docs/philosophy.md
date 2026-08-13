# Writing your one-pager

The advisor reads two things: your ledger, and a short document you write about
how you invest. The ledger it can compute — positions, cost basis, realized
P&L, concentration, fees. The second one it cannot guess, and without it every
observation it makes has to be hedged into uselessness.

A one-pager is what turns *"NVDA is 31% of your portfolio, which some investors
would consider concentrated"* into *"NVDA is 31%, and your own ceiling is 25% —
either trim ~$8k or write down why this position is the exception."* Same
numbers. The difference is that the second one can be acted on, because it is
measured against a rule you wrote when you were calm.

Nothing here leaves your machine except in the request you send to your own LLM
provider. It is stored in your own database, and the optional MCP server exposes
it at `portfolio://philosophy` to *your* agents only — nothing in this project
phones it anywhere.

## The fastest way to write one

Don't start from a blank page. Paste the prompt in
[docs/investor-interview.md](investor-interview.md) into any capable LLM, answer
its questions for twenty minutes, and paste the result into **Advisor → 📝
Investor one-pager**. Being interviewed is much easier than introspecting, and
the output is already shaped to the headings below.

Writing it by hand is fine too — start from
[philosophy.md.template](../philosophy.md.template).

## What makes each section usable

The test for every line: **could the advisor tell whether you are currently
obeying it?** If not, it's a mood, not a rule. Moods are pleasant to write and
produce nothing.

**North Star** — two to four sentences on what the money is *for*. Growth now
and preservation later is the usual arc; say roughly when the arc bends, and
what floor you refuse to go below. "Retire comfortably" is a mood. "Never risk
the 18 months of expenses in cash, whatever the setup looks like" is a rule.

**Core Philosophy** — three to five principles, each with a short expansion. The
expansion is where the value is, because it records the *reasoning*, which is
the part you'll lose access to under stress.

**Time Horizon** — how you separate capital you'd trade this quarter from
capital you intend to hold for a decade. The advisor uses this constantly: it is
what makes an unrealized loss on a core holding a non-event and the same loss on
a tactical position a decision.

**Mindset Rules** — numbered pre-commitments to your future self, written to be
executable while you are frightened. Give them numbers so the advisor can cite
them back at you: *"Rule 3 says you don't average down more than twice."*

**Personal Nuances** — the section people skip and then regret:

- **Tax** — your jurisdiction and the treatment that actually applies. The
  advisor cannot reason about *when* to realize a gain if it doesn't know
  whether realizing costs you 0% or 47%. This is the single highest-value line
  in the document.
- **Income and liquidity** — do you add monthly, or draw down? A portfolio being
  fed and a portfolio being harvested need opposite advice.
- **Time budget** — an hour a week and ten hours a week support very different
  strategies. Say which you have, honestly.
- **Blind spots** — the things you over-buy because you enjoy talking about
  them. Naming them lets the advisor push back on you specifically.
- **Family** — allocation constraints that aren't up for optimization.

**Anti-Portfolio** — what you won't buy regardless of upside, and why. This is
the cheapest section to write and the one that most improves the advice, because
it lets the advisor stop proposing things you were never going to do.

## Five ways this goes wrong

**1. Hedged language.** "I generally try to avoid excessive concentration"
cannot be checked. "No single position above 25% of the portfolio, no single
sector above 40%" can. Numbers where numbers are possible.

**2. Too many rules.** Twenty rules is a document you will violate and then
ignore. Five to eight real constraints beat a constitution you don't follow —
and a rule you have already broken twice is a rule to rewrite, not to keep.

**3. Generic risk talk.** "I am a long-term investor with moderate risk
tolerance" describes roughly everyone and constrains nothing. What did you
actually do in the last drawdown? Write *that* down, including the part you
aren't proud of.

**4. Writing for an imagined audience.** Nobody is grading this. If you write it
to sound like a disciplined investor rather than to describe yourself, the
advisor will reason about a person who doesn't exist, and its advice will fit
that person instead of you.

**5. Skipping tax.** By far the most common omission, and it silently degrades
every rebalancing suggestion you'll ever get.

## A complete fictional example

Invented persona and invented numbers — a shape to copy, not advice to follow.

```markdown
# Investor One-Pager — Dana R. (fictional)

**Last updated:** 2026-03-14
**Operating base:** Lisbon, Portuguese tax resident (NHR expired 2025)

## North Star
Compound aggressively until 2034, then shift toward preservation as my
daughter starts university. I want the freedom to leave a job I dislike
within a month of deciding to, which means liquidity matters more to me than
squeezing out the last few percent of return. Below €40k in cash I stop
adding risk, no matter how good the setup looks.

## Core Philosophy
- **Own businesses, not tickers** — if I can't explain how it earns money in
  two sentences, I don't own it.
- **Boring core, small edges** — 70% index, 30% conviction. The 30% is where
  I'm allowed to be interesting.
- **Losses are tuition, but only once** — I write down the reason for every
  sale at a loss, and I don't repeat the same mistake in a new ticker.

## Time Horizon
Core (index, ~70%): 10+ years, untouched, no tactical selling ever.
Conviction (~25%): 2–5 years, trimmed on thesis change, not on price.
Tactical (~5%): weeks to months, sized so a total loss is survivable.

## Mindset Rules
1. No buying in the first hour after a headline. Sleep on it.
2. Trim, don't exit: sell in thirds, never all at once.
3. Average down at most twice, then stop and re-examine the thesis.
4. Quarterly review only. No daily P&L checking — it makes me trade.
5. Any new position starts at half the size I want.

## Personal Nuances
- **Tax:** Portugal, 28% flat on capital gains, no long-term holding
  discount — so timing a realization for tax reasons alone is pointless
  here. Losses offset gains in the same year.
- **Income & liquidity:** salaried, adding €2,000/month. I do not draw from
  the portfolio and don't expect to before 2034.
- **Time budget:** about two hours a week, mostly Sunday evenings.
- **Public-narrative blind spot:** I over-buy semiconductors because I enjoy
  reading about them. Treat any new semi position as suspect.
- **Family:** €25,000 stays in short-term bonds for my daughter's tuition.
  Not investable, not to be optimized.

## Anti-Portfolio
What I won't buy regardless of upside:
- Leveraged or inverse ETFs — I've proven I hold them too long.
- Anything I first heard about on social media that week.
- Private placements I can't exit inside a week.

*Reviewed quarterly. Material changes require one week of cooldown before
execution.*
```

Note how much of it is numbers and refusals. That's what makes it usable.

## Where it's stored, and updating it

Paste it into **Advisor → 📝 Investor one-pager** and it's saved in your
database — included in `make backup`, and the expander title tells you which
source is in force.

A `philosophy.md` file next to your `docker-compose.yml` still works as a
fallback if you'd rather keep it in a file: the order is **database → mounted
file → nothing**, so anything saved in the dashboard wins. One catch worth
knowing about the file route: most editors save by writing a temporary file and
renaming it over the original, which replaces the file the container has open,
so **your edit stays invisible until you restart the container.** Pasting avoids
that entirely.

Revisit it after anything that changes the premises — a move, a new job, a
drawdown you handled badly. Rules written after a mistake are the good ones.
Rules written during one usually aren't.

An unedited template is detected and treated as absent, so the advisor won't
quietly reason about bracketed placeholders. Until you write one, briefs still
work; they're just generic.
