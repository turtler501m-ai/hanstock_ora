import asyncio

from src.dashboard import get_trades


async def main():
    print(await get_trades(50))


asyncio.run(main())
