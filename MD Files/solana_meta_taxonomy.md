# Solana Memecoin Launch Meta Taxonomy

Classification reference for **Zoey**. Each meta lists seed keywords the agent matches against a token's **name**, **ticker**, **description/metadata**, **image context**, and **linked socials**.

Matching notes:
- Match case-insensitively and on substrings in tickers (e.g. `DOGWIF` contains `dog`).
- Treat seeds as starting points. Zoey should add new seeds it discovers from coins that graduate.
- A coin can match several metas. Pick one primary label and up to two secondary labels, each with a confidence score.

---

## Summary

| # | Meta | Core idea | Examples | Typical behavior |
|---|------|-----------|----------|------------------|
| 1 | Animal | Pets, wildlife, animal mascots | BONK, WIF, POPCAT, MOODENG | Most durable meta. Many copies, few winners |
| 2 | Brainrot / TikTok | Absurd internet-native characters | GLORP, Italian brainrot coins | Fast, violent pumps, driven by TikTok |
| 3 | Viral moment / news | Something trending in the last 24–72h | MOODENG | First or "official" mint wins, copies die |
| 4 | Political / celebrity | Public figures and personalities | TRUMP | High impersonation and rug risk |
| 5 | AI / agent | AI models, bots, autonomous agents | GOAT | Comes in waves tied to AI news |
| 6 | Brand / IP-backed | Existing NFT or brand | PENGU | Slower, longer-lived, watch unlocks |
| 7 | Community / airdrop | Ecosystem-wide distribution | BONK | Rare now, survives cycles |
| 8 | Absurdist / cult | Irony, nihilism, "believe" culture | FARTCOIN, USELESS, SPX6900 | Conviction holders, long consolidations |
| 9 | Livestream / creator | Streamer or creator-launched coins | Creator coins | Tied to the stream, dies when it stops |
| 10 | Derivative / copycat | Spin-offs of a live runner | — | Parasitic, short-lived, signals parent peaking |
| 11 | Seasonal / event | Holidays, sports, scheduled events | — | Predictable timing |
| 12 | Regional / non-English | Non-English cultures and scripts | — | Clusters in waves |
| 13 | Crypto-native / Solana culture | Jokes about crypto itself | — | Insider humor, pumps with SOL sentiment |
| 14 | Finance / "fake utility" | Parodies of stocks, indices, companies | SPX6900 | Overlaps heavily with Absurdist |
| 15 | Food / object | Everyday items and food | — | Low-effort mints, occasional breakout |

---

## 1. Animal

**Name / ticker seeds**
```
dog, doge, dogwif, wif, inu, shib, puppy, pup, doggo, pupper, shiba, corgi, husky, pug,
cat, kitty, kitten, meow, popcat, mew, neko, tabby, chonk, floppa,
frog, pepe, toad, ribbit, froggy,
hippo, moodeng, pygmy, rhino, elephant, giraffe, zebra, lion, tiger, panther, leopard,
bear, panda, koala, sloth, otter, beaver, raccoon, possum, squirrel, hamster, capybara,
monkey, ape, chimp, gorilla, baboon, orangutan, lemur,
penguin, pengu, seal, walrus, whale, dolphin, shark, orca, octopus, crab, lobster, shrimp, fish,
bird, parrot, owl, eagle, duck, goose, chicken, rooster, pigeon, crow, flamingo,
goat, sheep, cow, bull, pig, horse, donkey, llama, alpaca, camel,
snake, lizard, gecko, turtle, tortoise, croc, gator, dino, trex, dragon,
bee, ant, spider, bug, worm, snail, butterfly, mouse, rat, bunny, rabbit, fox, wolf, deer
```

**Description seeds**
```
cutest, pet, adopted, rescue, zoo, sanctuary, wildlife, breed, good boy, good girl,
fluffy, furry, paws, woof, bark, purr, hops, waddle, mascot
```

**Emoji seeds:** 🐶 🐕 🐱 🐈 🐸 🦛 🐧 🦦 🐵 🦍 🐻 🐼 🦊 🐺 🐹 🐰 🦆 🦅 🐳 🦈 🐙 🦀 🐍 🐢 🦖

**Social signals:** pet-photo PFP, zoo or rescue account linked, animal video on TikTok/X

---

## 2. Brainrot / TikTok

**Name / ticker seeds**
```
brainrot, skibidi, rizz, gyatt, sigma, ohio, fanum, mewing, mogging, looksmax, aura,
npc, delulu, based, cooked, chopped, glaze, gooning, bussin, slay, yapping, yapper,
tralalero, tralala, bombardiro, crocodilo, tung, sahur, lirili, larila, brr brr, patapim,
chimpanzini, bananini, cappuccino, assassino, ballerina, trippi, troppi, bombombini, gusini,
glorp, alien cat, grimace, shrek, jeff, chungus, amogus, sus, hawk tuah, moo deng,
italian, italiano, unc, unc status, lowkey, highkey, no cap, fr, ong
```

**Description seeds**
```
tiktok, viral on tiktok, fyp, for you page, trend, sound, edit, brainrot, gen z, gen alpha,
italian brainrot, ai character, meme lore, cinematic universe
```

**Emoji seeds:** 🧠 💀 🗿 😭 🤯 👽 🍝 🇮🇹 🦈 🐊

**Social signals:** TikTok link in metadata, AI-generated character art, audio/sound references, high share of TikTok-sourced traffic

---

## 3. Viral moment / news

**Name / ticker seeds**
```
breaking, just happened, today, news, live, first, official, real, og, original, the,
justice for, rip, rest in peace, free, save, stop, ban, leak, leaked, exposed,
guy, girl, man, woman, kid, grandma, grandpa, uncle, auntie, dad, mom
```
(Plus any proper noun that matches a live trending-topic feed.)

**Description seeds**
```
trending, went viral, everyone is talking about, this just happened, news, headline,
breaking news, watch the video, clip, caught on camera, tweet, story, incident
```

**Emoji seeds:** 🚨 📰 🔥 📹 ⚡

**Social signals:** tweet or news URL in description; **a burst of same-name mints within minutes** (the strongest signal); keyword matches Google Trends / X trending in the last 72h

---

## 4. Political / celebrity

**Name / ticker seeds**
```
trump, donald, melania, barron, maga, potus, president, elon, musk, x ai, kanye, ye,
biden, harris, vance, rfk, obama, putin, zelensky, milei, bukele, modi, tinubu,
senator, congress, government, dept, doge department, election, vote, america, usa, patriot,
official, real, verified, ceo, founder, billionaire, celebrity, rapper, singer, actor,
footballer, messi, ronaldo, lebron, mrbeast, drake, taylor, swift, ishowspeed, kai cenat, adin
```

**Description seeds**
```
endorsed by, official token of, approved, the real, not affiliated (red flag),
politics, campaign, rally, supporter, fan token, tribute, parody
```

**Emoji seeds:** 🇺🇸 🏛️ 🗳️ 🦅 👑 🎤 ⚽ 🏀

**Social signals:** claims of endorsement; account names imitating verified figures (high impersonation risk); flag for manual verification

---

## 5. AI / agent

**Name / ticker seeds**
```
ai, agent, agents, gpt, llm, bot, neural, neuron, brain, mind, cortex, synapse, model,
autonomous, auto, sentient, agi, asi, singularity, terminal, truth, oracle, swarm, hive,
eliza, ai16z, goat, zerebro, virtuals, fartcoin (origin link), claude, grok, gemini, deepseek,
openai, anthropic, robot, robo, android, cyborg, droid, machine, compute, gpu, h100,
tensor, token, prompt, inference, training, dataset, vector, embedding, mcp, rag
```

**Description seeds**
```
ai agent, autonomous agent, powered by ai, trained on, fine-tuned, runs on, self-aware,
onchain agent, ai trader, ai influencer, framework, github, open source, sdk, api
```

**Emoji seeds:** 🤖 🧠 💻 ⚙️ 🛰️ 👾

**Social signals:** GitHub repo link, an X account that posts autonomously, technical docs or whitepaper link

---

## 6. Brand / IP-backed

**Name / ticker seeds**
```
pengu, pudgy, penguins, azuki, milady, remilio, degods, y00ts, mad lads, claynosaurz,
smb, okay bears, bored ape, nft, collection, ip, brand, studio, official token, ecosystem token
```

**Description seeds**
```
nft collection, holders, airdrop to holders, ip, licensing, merchandise, toys, retail,
brand, franchise, team, roadmap, partnership, official launch
```

**Social signals:** doxxed team, official website, linked existing NFT collection with history, verified brand accounts

---

## 7. Community / airdrop

**Name / ticker seeds**
```
community, people, the people's, dao, collective, fam, frens, gm, wagmi, together,
solana, sol, saga, seeker, phantom, backpack, jupiter, jup
```

**Description seeds**
```
airdrop, airdropped, claim, eligible, distributed to, community owned, fair launch,
no team allocation, burn, governance, vote, dao treasury
```

**Emoji seeds:** 🪂 🤝 🌐 🗳️

**Social signals:** airdrop claim site, DAO or governance page, broad initial holder distribution

---

## 8. Absurdist / cult

**Name / ticker seeds**
```
useless, nothing, nobody, none, zero, void, null, meaning, meaningless, pointless, nihil,
fart, fartcoin, poop, butt, burp, sneeze, toilet, diaper,
cult, believe, belief, faith, church, religion, prophet, gospel, pray, ascend, transcend,
retard, retardio, bozo, clown, idiot, stupid, dumb, loser, cope, seethe, copium,
just a, literally, it's over, we're so back, wagmi, ngmi, send it, lfg, moon, to the moon
```

**Description seeds**
```
no utility, zero utility, no roadmap, no promises, just a meme, just vibes, for fun,
join the cult, believers only, higher, we believe, conviction, holders never sell
```

**Emoji seeds:** 💨 🤡 🙏 🕯️ ⛪ 🫠 💩

**Social signals:** heavy meme-art output, "cult" or "believer" language, raid culture on X

---

## 9. Livestream / creator

**Name / ticker seeds**
```
stream, streamer, live, ttv, kick, twitch, youtube, yt, vlog, creator, influencer,
podcast, show, episode, challenge, 24/7, marathon, subathon, irl
```

**Description seeds**
```
live now, watch me, streaming, i will, if we hit, market cap goal, mc goal, challenge,
creator coin, my coin, my token, community of, subscribers, followers
```

**Emoji seeds:** 🔴 🎥 📺 🎙️ 🎮

**Social signals:** pump.fun livestream flag active; creator's own socials linked; dev wallet tied to a known creator

---

## 10. Derivative / copycat

**Name / ticker seeds**
```
baby, mini, little, junior, jr, son of, daddy, mommy, wife of, sister, brother, cousin,
2.0, v2, 2, ii, classic, real, og, the real, community, cto,
chinese, japanese, korean, black, white, gold, dark, evil, based, super, mega, giga, ultra,
inverse, anti, un, not, fake, wrapped, sol, on sol
```
(Plus the ticker or name of any token currently in the top runners, followed by one of the modifiers above.)

**Description seeds**
```
inspired by, the next, sister coin, little brother of, missed, second chance, this time,
community version, the real one
```

**Social signals:** description references another live token; launched shortly after the parent pumped. Use this meta as a **peak signal for the parent**.

---

## 11. Seasonal / event

**Name / ticker seeds**
```
halloween, spooky, pumpkin, ghost, witch, zombie, skeleton, vampire,
christmas, xmas, santa, elf, reindeer, snow, winter, grinch,
newyear, nye, 2027, valentine, love, easter, bunny, thanksgiving, turkey,
ramadan, eid, sallah, diwali, lunar, cny, dragon year, snake year, horse year,
superbowl, world cup, worldcup, olympics, champions league, finals, nba, nfl, ufc, fight,
halving, fomc, cpi, rate cut, election day, black friday, summer, spring
```

**Description seeds**
```
celebrate, season, holiday, this year, event, countdown, tonight, match, game day
```

**Emoji seeds:** 🎃 👻 🎄 🎅 ❄️ 🎆 💘 🐣 🦃 🌙 🏆 ⚽ 🏈

**Social signals:** launch date within ~2 weeks of the event

---

## 12. Regional / non-English

**Name / ticker seeds**
```
Non-Latin script in name/ticker (Chinese, Japanese, Korean, Arabic, Cyrillic, Thai, Devanagari),
chinese, china, cn, asia, japan, jp, korea, kr, india, desi, naija, nigeria, africa, afro,
latam, brazil, br, mexico, turkey, tr, vietnam, vn, indonesia, id, philippines, pinoy,
arab, dubai, russia, ru, ukraine, europe, eu
```

**Description seeds**
```
Descriptions written mainly in a non-English language, local slang, regional memes,
local celebrities, local holidays
```

**Social signals:** non-English socials, regional Telegram/WeChat/KakaoTalk groups, region-specific exchange listings

---

## 13. Crypto-native / Solana culture

**Name / ticker seeds**
```
sol, solana, jeet, jeets, degen, ape, aped, rug, rugged, rugpull, dev, dev sold, cto,
bags, bagholder, exit liquidity, pump, dump, pumpfun, bonding curve, graduate, raydium,
jupiter, phantom, wallet, seed phrase, gm, gn, ser, anon, fren, wagmi, ngmi, hodl,
diamond hands, paper hands, moon, lambo, wen, wen lambo, bull, bear, crab market,
satoshi, bitcoin, btc, eth, vitalik, cz, binance, coinbase, sec, gensler
```

**Description seeds**
```
for the degens, trenches, in the trenches, jeets will cry, dev is based, no dev,
community takeover, send it, 1000x, next 100x
```

**Emoji seeds:** 💎 🙌 📈 📉 🚀 🌕 🦍

---

## 14. Finance / "fake utility"

**Name / ticker seeds**
```
spx, spx6900, index, etf, stock, stocks, nasdaq, dow, s&p, fund, capital, bank, reserve,
fed, treasury, bond, dollar, usd, stable, stablecoin, gold, oil, commodity,
inc, corp, llc, holdings, group, enterprises, labs, ventures, strategy, microstrategy,
coin, money, cash, pay, finance, trade, trader, alpha, yield
```

**Description seeds**
```
flip the stock market, better than stocks, parody of, not financial advice, fake company,
the world's first, reserve currency, store of value
```

**Emoji seeds:** 📊 💵 🏦 💹 🪙

---

## 15. Food / object

**Name / ticker seeds**
```
pizza, burger, taco, sushi, ramen, noodle, rice, bread, toast, egg, cheese, butter, bacon,
banana, apple, orange, lemon, mango, avocado, potato, tomato, onion, garlic, chili, pepper,
coffee, tea, milk, beer, wine, soda, cola, juice, water, ice, cookie, cake, donut, candy,
jollof, suya, puff puff, chin chin, chair, table, rock, stone, stick, brick, spoon, cup,
hat, shoe, sock, sunglasses, shades, phone, car, truck, rocket, balloon, button, box
```

**Description seeds**
```
just a, simple, everyday, delicious, tasty, hungry, snack, object, thing
```

**Emoji seeds:** 🍕 🍔 🌮 🍣 🍌 🥑 ☕ 🍺 🍩 🪨 🧢 👟

---

## Classification rules for Zoey

1. **Multi-label.** Assign one primary meta plus up to two secondary metas, each with a confidence score from 0 to 1. Example: MOODENG is primary Animal, secondary Viral moment.
2. **Weighting.** Weight matches roughly as name/ticker > description > image/social > emoji. An exact ticker match counts more than a substring match.
3. **Unclassified bucket.** Below the confidence threshold (start around 0.4), label the coin `unclassified` instead of forcing a label. Clustering the unclassified coins is how Zoey discovers new metas.
4. **Seed growth.** When a coin graduates, extract new tokens from its name and description and propose them as seed candidates for its meta. Review them before promoting.
5. **Heat tracking.** Per meta, track its daily share of new mints and its share of graduations. When a meta's graduation rate rises faster than its mint rate, it is heating up.
6. **Decay.** Most tokens die within 3–8 weeks, so compute meta heat on a rolling window (e.g. 7 and 30 days) instead of all-time totals.
7. **Copycat check.** Before assigning a Derivative label, compare the name against currently trending tokens. If it matches, link the coin to its parent.
