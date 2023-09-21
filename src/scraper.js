const cheerio = require("cheerio");
const axios = require("axios");
const router = require("express").Router();
const { generateFilename, saveProductJson, generateTitle } = require("./utils");

const baseURL = "https:///www.amazon.com";

router.post('/scrape', async (req, res) => {
    console.log(req)

    const { url } = req.body;

    if (!url.includes(baseURL)) {
        console.log('Invalid URL')
        return
    }
    try {

        axios.get(baseURL).then((response) => {
            console.log('get', response)
            const $ = cheerio.load(response.data);
            const products = [];

            $('.s-result-item').each((i, el)  => {
                const product = $(el);

                const priceWhole = product.find('.a-price-whole').text();
                const priceFraction = product.find('.a-price-fraction').text();
                const price = priceWhole + priceFraction;
                
                const link = product.find(".a-link-normal.a-text-normal").attr('href');
                const title = generateTitle(product, link);

                if (title !== "" && price !=="" && link !=="") {
                    product.push({ title, price, link });
                }

            })

            saveProductJson(products);

            res.json({
                products_saved: products.length,
                message: "Products scraped successfully",
                filename: generateFilename()
            });

        });
    } catch (error) {
        res.statusCode(500).json({
            message: "Error scraping products",
            error: error.message,
        })
    }

})