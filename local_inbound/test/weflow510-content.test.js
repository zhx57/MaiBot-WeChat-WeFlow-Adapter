"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  extractMentionUserIds,
  parseMessage,
} = require("../lib/weflow510-content");
const { WechatStore, isOutgoingMessage } = require("../lib/wechat-store");

test("mentions inside a quoted message do not become top-level mentions", () => {
  const content =
    "<refermsg><msgsource><atuserlist>wxid_nested</atuserlist></msgsource></refermsg>" +
    "<msgsource><atuserlist>wxid_direct</atuserlist></msgsource>";

  assert.deepEqual(extractMentionUserIds(content), ["wxid_direct"]);
});

test("native bot wxid identifies only self-sent messages", () => {
  const botId = "wxid_vt620cuy5hio22";

  assert.equal(isOutgoingMessage({ is_send: 0 }, botId, botId), true);
  assert.equal(
    isOutgoingMessage({ is_send: 0 }, "wxid_ev4txoa8f2mm22", botId),
    false,
  );
});

test("self profile exposes the native bot identity and avatar", () => {
  const store = Object.create(WechatStore.prototype);
  store.myWxid = "wxid_vt620cuy5hio22";
  store.contactInfo = (wxid) => ({
    displayName: wxid === store.myWxid ? "菲比" : wxid,
    avatarUrl: "https://wx.qlogo.cn/bot-avatar/0",
  });

  assert.deepEqual(store.selfProfile(), {
    userId: "wxid_vt620cuy5hio22",
    nickname: "菲比",
    avatarUrl: "https://wx.qlogo.cn/bot-avatar/0",
  });
});

test("link cards retain their title, description, and URL", () => {
  const parsed = parseMessage({
    local_type: 49,
    message_content:
      "<msg><appmsg><type>5</type><title>Docs</title><des>Reference</des>" +
      "<url>https://example.com/docs</url></appmsg></msg>",
  });

  assert.equal(parsed.app.kind, "link");
  assert.match(parsed.display, /Docs/);
  assert.match(parsed.display, /Reference/);
  assert.match(parsed.display, /https:\/\/example\.com\/docs/);
});

test("file, location, and video metadata remain structured", () => {
  const file = parseMessage({
    local_type: 49,
    message_content:
      "<msg><appmsg><type>6</type><title>report.pdf</title><totallen>4096</totallen>" +
      "<md5>0123456789abcdef0123456789abcdef</md5></appmsg></msg>",
  });
  assert.equal(file.messageType, "file");
  assert.equal(file.app.fileSize, 4096);

  const location = parseMessage({
    local_type: 48,
    message_content:
      '<msg><location label="Square" poiname="Station" x="31.23" y="121.47" scale="16" /></msg>',
  });
  assert.equal(location.locationInfo.poiname, "Station");
  assert.match(location.display, /31\.23,121\.47/);

  const video = parseMessage({
    local_type: 43,
    message_content: '<msg><videomsg playlength="9" length="2048" /></msg>',
  });
  assert.equal(video.videoInfo.duration, 9);
  assert.equal(video.videoInfo.size, 2048);
});

test("APK canonical inbound message types stay aligned", () => {
  const cases = [
    [1, "hello", "text"],
    [3, "<msg><img /></msg>", "image"],
    [34, "<msg><voicemsg /></msg>", "voice"],
    [43, '<msg><videomsg playlength="1" /></msg>', "video"],
    [47, '<msg><emoji md5="abc" /></msg>', "emoji"],
    [48, '<msg><location label="Here" x="1" y="2" /></msg>', "location"],
    [42, '<msg username="wxid_card" nickname="Card User" />', "contact_card"],
    [49, '<msg><appmsg><type>5</type><title>Link</title></appmsg></msg>', "link"],
    [49, '<msg><appmsg><type>33</type><title>Mini</title></appmsg></msg>', "mini_program"],
    [49, '<msg><appmsg><type>19</type><title>Record</title></appmsg></msg>', "chat_record"],
    [49, '<msg><appmsg><type>2000</type><feedesc>¥1.00</feedesc></appmsg></msg>', "transfer"],
    [49, '<msg><appmsg><type>2001</type><nativeurl>wxpay://hongbao</nativeurl></appmsg></msg>', "red_packet"],
    [49, '<msg><appmsg><type>51</type><finderFeed><desc>Video Channel</desc></finderFeed></appmsg></msg>', "video_channel"],
    [266287972401, '<sysmsg><patMsg><title>拍了拍</title></patMsg></sysmsg>', "pat"],
    [10000, '<sysmsg type="revokemsg"><revokemsg><newmsgid>123</newmsgid><replacemsg>撤回了一条消息</replacemsg></revokemsg></sysmsg>', "recall"],
    [10000, '<sysmsg><sysmsgtemplate><content_template><plain>修改群名</plain></content_template></sysmsgtemplate></sysmsg>', "system"],
  ];

  for (const [localType, messageContent, expected] of cases) {
    const parsed = parseMessage({ local_type: localType, message_content: messageContent });
    assert.equal(parsed.messageType, expected, `${localType} should map to ${expected}`);
  }

  const reply = parseMessage({
    local_type: 49,
    message_content:
      "<msg><appmsg><type>57</type><title>reply</title><refermsg><type>1</type>" +
      "<svrid>123</svrid><content>original</content></refermsg></appmsg></msg>",
  });
  assert.equal(reply.messageType, "reply");
});
