# The story behind Shuttle & Valet Ops

## The ride that started it

I took my car to an authorized dealership for maintenance. They offered a shuttle, so I rode it to a spot near my home. When the car was ready, I was told the driver would call me and pick me up where I had been dropped off, and I was given a departure time.

I waited for exactly one hour. In that hour the driver and I spoke on the phone three times — "almost there", "on my way". Since it was a shuttle, he was picking up other customers too. After an hour he called to say he had arrived. He wasn't there; he had come to the street behind me.

On the way to the next customer he was on the phone constantly, and we nearly had an accident two or three times. He had forgotten someone, so there were more calls and turning back. Everyone in the van was unhappy. I asked the driver what it would be like if there were an app and everything showed up automatically. "That would be great," he said. For me, that is how the project started.

Commercially, that was also my biggest mistake: I started building on the word of someone who was not the decision-maker. Because I had lived through the problem myself, everything made sense to me.

## Building the shuttle module

I designed the architecture for the shuttle first: what it needed to do and how I should move forward. I went step by step and tested at the end of every step. The first version of the shuttle product took about two months. Up to that point I used AI only to check my code.

Then came deployment, which I had never done before. There I used AI as a guide — which tools to use and how to set things up.

## The first pitch

Once I was confident in the product, I went straight back to the dealership and gave a presentation to the service manager. He liked it a lot, but he was not the decision-maker either. It was a corporate group with several brands; they sent my presentation to headquarters in Istanbul, and I never heard back.

Meanwhile I kept talking to other dealerships, and that is where I felt the first real pain: very few of them run a shuttle at all, and those that do were cutting it back for cost reasons. And no one was buying the service from me. By then three months had passed.

## Looking for a market

My first idea was school transport: parents, the school and the bus company could all see where a student was. But a new regulation was making tracking devices mandatory in school vehicles, and most of those devices — even if worse than my product — already covered the basics, so no one wanted to pay extra.

I went back to automotive. Shuttles were rare, but valet service (picking up a customer's car from their door and returning it) was much more common at service centres. To have a demo in hand, I built the valet module on top of my own shuttle architecture, this time with heavy AI assistance. The shuttle had taken me two months; with AI, the valet system was about 90% in place within one to two weeks.

I started meeting companies again. This time the obstacle was habit. Nothing was recorded; work ran over the phone and WhatsApp, and nobody was unhappy with that. The one or two companies that liked the project said they had no budget for it. I talked to many more companies, including outside my city. Sometimes there was no answer at all, and the answers that came were negative.

As my hope was running out, a friend pointed me to logistics. Freight companies working with subcontracted drivers were calling them to find out where the vehicle and the load were. I could automate that, and my system could cover freight with relatively little change. Some parts of the code would have needed work, but not having sold a single product by then was exhausting.

## Shutting it down

Early on, because I believed in the project, I applied to Ege Teknopark in İzmir. Access to their R&D application portal took months, so the full application went in in July 2026 with a two-year R&D plan: personalising arrival-time predictions with each driver's behaviour, and re-planning routes during the day without breaking times already promised to passengers. The project was accepted, and the contract was due in October. But with no revenue — only one or two companies keen on a free trial — I decided not to found the company and did not sign.

In total I talked to more than 50 companies across automotive dealerships, insurance, roadside assistance and freight. None of them paid. In October 2026 I shut the product down.

## What it taught me

This project was one of the biggest experiences of my life. I handled everything, from the first sketch to the production server, alone. It taught me a lot on the code side and just as much on the business side:

- Talk to the person who pays before building. A driver's enthusiasm is not demand.
- In many traditional businesses, the phone and WhatsApp are "good enough"; a better tool has to solve a problem that costs the buyer real money.
- Interest in a free trial is not the same as willingness to pay.
- Owning the architecture paid off: once the shuttle was done, a second module took weeks, not months.
