FROM rl-exploits-foundry-core:latest

RUN cp -R /opt/rl-foundry-project/. /app/
COPY --chown=rltool:rltool project/ /app/
RUN forge build --offline && rm -rf cache out && chown -R rltool:rltool /app

ENV FORK_BLOCK_NUMBER=24979315
ENV FORK_EXPECTED_BLOCK_NUMBER=24979316
ENV FORK_TARGET_TIMESTAMP=1777388411
