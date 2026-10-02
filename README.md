# Inner-speech weekly literature digest

A small GitHub Actions workflow that:

1. searches **PubMed**, **bioRxiv**, and **OpenAlex**;
2. retrieves recent papers related to:
   - inner speech
   - inner speech diversity
   - anendophasia
   - auditory verbal aphantasia
   - endophasia
3. deduplicates results;
4. applies a transparent rule-based relevance score;
5. posts the highest-ranked papers to Discord.
