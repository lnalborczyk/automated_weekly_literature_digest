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

## Cost

The pipeline itself does not require any paid API.

- PubMed API: free
- bioRxiv API: free
- OpenAlex API: free for this level of use
- Discord webhook: free
- GitHub Actions: typically covered by GitHub's included Actions allowance for a tiny weekly workflow
