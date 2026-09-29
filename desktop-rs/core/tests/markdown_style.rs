use clarp_core::markdown_style::{StyleOptions, cache_key, options_from, styled_markdown_html};
use serde_json::json;

const FIXTURE: &str = "# Title\n\nBody with `inline` code and a [link](https://example.com).\n\n## Second\n\n```cpp\nint main() {}\n```\n\n> quoted\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n";

fn options() -> StyleOptions {
    StyleOptions { body_pixel_size: 16, ..StyleOptions::default() }
}

#[test]
fn headings_scale_with_the_body_not_the_web() {
    let html = styled_markdown_html(FIXTURE, &options());
    assert!(html.contains("font-size:21px; font-weight:600;\">Title"), "h1 at 1.3x of 16: {html}");
    assert!(html.contains("font-size:19px; font-weight:600;\">Second"), "h2 at 1.18x");
    assert!(!html.contains("<h1") && !html.contains("<h2"), "no heading tags: the viewer would scale them again");
}

#[test]
fn code_uses_mono_and_background() {
    let options = StyleOptions { mono_family: "Mono Test".into(), code_background: "#123456".into(), ..options() };
    let html = styled_markdown_html(FIXTURE, &options);
    assert!(html.contains("background-color:#123456;\"><span style=\"font-family:'Mono Test'; font-size:14px;\">int main() {}</span></pre>"), "{html}");
    let block = styled_markdown_html("```\na\n\nb\n```\n", &options);
    assert_eq!(block.matches("background-color:#123456;").count(), 3, "every code line, blank ones too, has the background: {block}");
    assert!(block.contains("&nbsp;"));
    assert!(html.contains("font-family:'Mono Test'; font-size:14px; background-color:#123456;\">inline</span>"), "inline code keeps its tint");
    assert!(html.contains("<a href=\"https://example.com\"><span style=\"color:#82aaff; text-decoration: underline;\">link"));
}

#[test]
fn quotes_lists_and_tables_are_styled() {
    let options = StyleOptions { quote_text: "#abcdef".into(), ..options() };
    let html = styled_markdown_html(FIXTURE, &options);
    assert!(html.contains("margin-left:14.4px;\"><span style=\"color:#abcdef;\">quoted"), "{html}");
    assert!(html.contains("-qt-list-indent: 1;\"><li"));
    assert!(html.contains("<table border=\"0.5\" cellspacing=\"0\" cellpadding=\"5\" style=\"border-color:#303342;"));
    assert!(html.contains("<th>a</th>") && html.contains("<td>2</td>"));
}

#[test]
fn styled_html_carries_the_final_layout() {
    let options = StyleOptions { code_background: "#123456".into(), ..options() };
    let html = styled_markdown_html("# Title\n\nText with `code`.\n\n```\nblock\n```\n", &options);
    assert!(html.contains("font-size:21px"));
    assert!(html.contains("#123456"));
    assert!(!html.contains("font-family:;") && !html.contains("font-family:'';"));
    assert!(html.contains("<p style=\"margin-top:0px; margin-bottom:3.2px;\">"), "the first heading has no air above");
}

#[test]
fn raw_html_in_a_message_stays_text() {
    let html = styled_markdown_html("hi <script>alert(1)</script> <b>bold</b>", &options());
    assert!(!html.contains("<script>") && html.contains("&lt;script&gt;") && html.contains("&lt;b&gt;"));
}

#[test]
fn large_messages_with_mixed_inline_formats_render() {
    let mut markdown = String::from("# Head `a` [l](https://x.y) **b** `c`\n\n");
    for i in 0..400 {
        markdown += &format!("Line {i} with `code` and [link](https://e.com/{i}) and *em* `more` text.\n\n");
    }
    let html = styled_markdown_html(&markdown, &StyleOptions { mono_family: "Mono Test".into(), ..options() });
    let last = &html[html.find("Line 399").unwrap()..];
    assert!(last.contains("'Mono Test'"), "code styled to the end");
}

#[test]
fn options_parse_from_qml() {
    let values = json!({"bodyPixelSize": 17, "link": "#1f7a78", "codeBackground": "not a colour", "bodyFamily": "Atkinson"});
    let options = options_from(values.as_object().unwrap());
    assert_eq!(options.body_pixel_size, 17);
    assert_eq!(options.link, "#1f7a78");
    assert_eq!(options.code_background, "#20212e", "invalid input keeps the default");
    assert_eq!(options.body_family, "Atkinson");
    assert_eq!(options_from(json!({"bodyPixelSize": 400}).as_object().unwrap()).body_pixel_size, 64);
    let a = cache_key("x", json!({"a": 1, "b": "c"}).as_object().unwrap());
    assert_eq!(a, cache_key("x", json!({"b": "c", "a": 1}).as_object().unwrap()), "order-independent");
}
