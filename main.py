import asyncio

from rpa import AsyncCamoufoxClient


def main():
    try:
        asyncio.run(AsyncCamoufoxClient().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
