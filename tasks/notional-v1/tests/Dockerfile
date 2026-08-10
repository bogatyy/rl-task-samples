FROM rl-exploits-notional-v1:latest

USER root
COPY test.sh /tests/test.sh
COPY ExploitGrader.sol /tests/ExploitGrader.sol
RUN chmod +x /tests/test.sh

ENV TASK_ID=notional-v1
ENV KEEP_ARCHIVE_RPC=1

USER rltool
ENTRYPOINT []
CMD ["/tests/test.sh"]
