FROM rl-exploits-foundry-core:latest

RUN cp -R /opt/rl-foundry-project/. /app/
COPY --chown=rltool:rltool project/ /app/
RUN forge build --offline && rm -rf cache out && chown -R rltool:rltool /app

ENV CHAIN=8453
ENV CHAIN_ID=8453
ENV ANVIL_HARDFORK=cancun
ENV FORK_BLOCK_NUMBER=40229652
ENV FORK_EXPECTED_BLOCK_NUMBER=40229652
