const cheerio = require("cheerio");
const axios = require("axios");
const router = require("express").Router();
const { generateFilename, saveProductJson, generateTitle } = require("./utils");

const baseURL = "https:///www.amazon.com";

router.post('/scrape', async (req, res) => {
    console.log(req)

    // const { url } = req.body;


    try {

        axios.get(baseURL).then((response) => {
            console.log('get', response)
        })
    } catch (error) {
        console.log(error);
    }

})