const page = new Page();
await page.goto("https://example.com");

const result = page.extract({
  title: "title",
  heading: "h1",
  description: "p"
});

console.log(JSON.stringify(result));
