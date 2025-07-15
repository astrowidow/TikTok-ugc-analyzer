from TikTokApi import TikTokApi
import asyncio
import os

ms_token = os.environ.get("ms_token", None) # get your own ms_token from your cookies on tiktok.com

async def trending_videos():
    async with TikTokApi() as api:
        await api.create_sessions(ms_tokens="C09EEkIAC5vhF66kGJ3pYw2FNeLtWJnh_7XoskUSDoA_ftxzYuIH7Q488xrqSozSQmg2Sr_IkftyCyL1aTUHPFOXGCrVIVdAjDEOnvDtfbIBHbeW1eeMR763ULMPDOF4FKXu8_-qgg0-JXKwTjtv3d4=", num_sessions=1, sleep_after=3)
        async for video in api.trending.videos(count=30):
            print(video)
            print(video.as_dict)

if __name__ == "__main__":
    asyncio.run(trending_videos())

