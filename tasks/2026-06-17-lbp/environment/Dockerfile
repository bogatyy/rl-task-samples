FROM rl-exploits-foundry-core:latest

RUN cp -R /opt/rl-foundry-project/. /app/
COPY --chown=rltool:rltool project/ /app/
RUN forge build --offline && rm -rf cache out && chown -R rltool:rltool /app

ENV CHAIN_ID=56
ENV ANVIL_HARDFORK=cancun
ENV FORK_BLOCK_NUMBER=104727183
