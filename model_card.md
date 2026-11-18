# Model Card: PawPal+ Sitter


## System overview


PawPal+ Sitter allows the owner to give care instructions that are used in a RAG way for the sitter. It also uses agents to fix schedule conflicts that the sitter has in terms of the care schedule.


## Intended use and users

It is intended to be used between owners of pets and people who would like to take care of them. Friends, coworkers, or even if a pound would like to foster a pet to someone else, they can pass on the care instructions and the user can determine if they can handle that kind of pet.

---

## 1. How did you collaborate with AI on this project?

I gave AI a bird's-eye view of what I wanted the app to accomplish, as well as what my average happy path looked like. It was able to come up with a mock up that we adjusted, as well as using the old diagrams we adjusted until we got to the finalpawpal.mmd.

## 2. One AI suggestion you accepted

Originally I thought about having the database in Supabase, because that was what I was used to. However AI had come up with an offline plan that saved the data in JSON and I felt like this was more usable for the demo that I would have to present, and for future uses, as Supabase tends to close the tables of unused projects.

## 3. One AI suggestion you rejected or revised

It wanted a lot of required fields for the owner, but since I wanted just a working model without too much real data (so I didn't want to put in a real vet, real email, real phone number), it was good enough to make those optional. It's also possible that if it was a lost pet or a pound pet, it would not have a ready vet or owner number to have.

## 4. Limitations of the system

It misses paraphrases. For example, "Mochi skipped his dinner, should I worry?" returns the dinner routine instead of the appetite guide. hit@1 is 82% on the evaluation set. The emergency and medication checks match phrases, so unusual wording could slip past. If the owner writes something wrong, the app repeats it.

In the end, I have limited knowledge and the program really depends on what the users put in, so the adage "garbage in, garbage out" applies here. 

## 5. Risks, misuse, and safeguards

Much like the last question, the owner could be wrong about something, or not specify an important fact. There are some safeguards, even more if you managed to have a real AI api key to use, but there are at least some vet emergency contacts and medicines that are common knowledge that it can catch.

## 6. Testing and evaluation results


> *Facts:*
> - `python -m pytest` runs 74 passing tests.
> - `python -m evaluation.evaluate` scores:
>   - planner scenarios 100%
>   - retrieval hit@3 95% (hit@1 82%, MRR 0.88)
>   - guardrails 100%
>   - conflict resolver 100%

I also tested manually to see how it would make a schedule and everything checked out.

## 7. What would you improve next?

I would really like to improve the UI, I think it could use some work to make it more friendly to see the schedule or add specifications. In addition, some pets rely on each other, like I can feed two pets at the same time but perhaps one pet cannot be in the same room as the other when it's eating. 


