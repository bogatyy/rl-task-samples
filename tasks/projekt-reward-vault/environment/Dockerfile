FROM rl-exploits-foundry-core:latest

RUN cp -R /opt/rl-foundry-project/. /app/
COPY --chown=rltool:rltool project/ /app/
RUN forge build --offline && rm -rf cache out && chown -R rltool:rltool /app

ENV FORK_BLOCK_NUMBER=25606411
ENV FORK_EXPECTED_BLOCK_NUMBER=25606412
ENV FORK_REPLAY_TRANSACTION_HASHES=0xe4b70d3c3e745237b92212750763e69da88a79cd061cf9cc8f050b02cb1f0892,0x65c0732bac3590f8581669f49a55a0e32079ed77e050f243d772a8984c82641a
