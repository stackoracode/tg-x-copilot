from types import SimpleNamespace as NS

from tg_x_copilot.models import MediaKind
from tg_x_copilot.pipeline.normalizer import normalize


def msg(id, text="", photo=None, document=None, file=None, grouped_id=None, fwd_from=None):
    return NS(id=id, raw_text=text, photo=photo, document=document, file=file,
              grouped_id=grouped_id, fwd_from=fwd_from, sticker=None, gif=None)


def test_album_merges_caption_and_media():
    fwd = NS(from_id=NS(channel_id=12345), from_name=None, post_author=None, channel_post=7,
             date=None)
    msgs = [
        msg(11, "Caption here", photo=object(), file=NS(size=1000), grouped_id=99, fwd_from=fwd),
        msg(10, "", photo=object(), file=NS(size=2000), grouped_id=99, fwd_from=fwd),
        msg(12, "", document=NS(mime_type="video/mp4"),
            file=NS(mime_type="video/mp4", size=5, name="v.mp4"), grouped_id=99, fwd_from=fwd),
    ]
    env = normalize(msgs, chat_id=1, user_id=2, locale="en-US", market="US")
    assert env.message_ids == [10, 11, 12]
    assert env.text == "Caption here"
    assert [m.kind for m in env.media] == [MediaKind.PHOTO, MediaKind.PHOTO, MediaKind.VIDEO]
    assert env.grouped_ids == [99]
    assert len(env.forwards) == 1 and env.forwards[0].post_id == 7
    assert env.forwards[0].chat_id == -10012345


def test_burst_of_text_messages_joined():
    env = normalize([msg(1, "first"), msg(2, "second"), msg(3, "first")], chat_id=1, user_id=2,
                    locale="en-US", market="US")
    assert env.text == "first\n\nsecond"
    assert env.media == []
